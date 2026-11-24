"""분류 희망 폴더 목록 → 정리 대상 폴더 → 저장 폴더."""

from __future__ import annotations

from datetime import date
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from lxml.etree import XMLSyntaxError
from openpyxl.utils.exceptions import InvalidFileException

from tongdory2.branding import FOLDER_LIST_STEM
from tongdory2.fsx.paths import first_symlink_component, is_within, real, tilde
from tongdory2.fsx.scan import SKIP_DIRS
from tongdory2.spec.normalize import TOP_IDS, parse
from tongdory2.fsx.roots import Decision, RegisteredRoot, Role, STORAGE_DEFAULT_NAME, check_root, is_denied_path
from tongdory2.fsx.scope import Scope
from tongdory2.fsx.storage import ensure_layout
from tongdory2.messages import msg
from tongdory2.spec.structure_input import (
    DEFAULT_TEMPLATE_ENTRIES,
    MAX_DOCUMENT,
    document_paths,
    export_structure_json,
    export_structure_txt,
    extraction,
    folder_paths,
    format_editor_lines,
    parse_editor_text,
    sanitize_filename,
)
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import templates
from tongdory2.web.export_io import export_text_or_save
from tongdory2.web.progress import folder_tree
from tongdory2.web.setup_state import pending_setup_step, scope_key, settings_change_notice

router = APIRouter(prefix="/setup")
log = logging.getLogger(__name__)
STEPS = {
    "structure": "분류 희망 폴더 목록",
    "sources": "정리 대상 폴더",
    "destination": "저장 폴더",
}


def _path(text: str, t) -> Path:
    value = text.strip()
    if value == "~" or value.startswith("~/"):
        value = str(t.home) + value[1:]
    p = Path(value)
    if not p.is_absolute() or ".." in p.parts:
        raise ValueError("전체 폴더 경로를 입력해 주세요. 예: ~/Documents/개발자료")
    if first_symlink_component(p, Path(p.anchor)):
        raise ValueError("링크 경로 대신 실제 폴더를 선택해 주세요.")
    return real(p)


def _readable(p, t) -> bool:
    return not is_denied_path(p, t.home, t.app_data) and os.access(p, os.R_OK | os.X_OK)


def _render(request: Request, step: str, *, error: str = "", **context):
    t = request.app.state.t
    flash, t.flash = t.flash, None
    roots = t.db.list_roots()
    store = SpecStore(t.db.conn)
    spec = store.current()
    structure = spec
    selected = [tilde(r.path, t.home) for r in roots if r.role == Role.COLLECT]
    destination = next((tilde(r.path, t.home) for r in roots if r.role == Role.CURRENT), "")
    nodes = [n for n in structure["body"]["nodes"] if n["status"] == "ADOPTED" and n["role"] == "DEST"] if structure else []
    formatted_paths = format_editor_lines([(n["display_path"], bool(n.get("required_for_handover"))) for n in nodes])
    return templates.TemplateResponse(request, "setup.html", {
        "stage": step, "stages": STEPS, "error": error, "flash": flash, "revision": scope_key(roots, spec),
        "paths": formatted_paths,
        "required": [n["display_path"] for n in nodes if n.get("required_for_handover")],
        "reference": structure["body"].get("structure_reference", "") if structure else "",
        "sources": "\n".join(selected), "destination": destination,
        "default_template_text": format_editor_lines(list(DEFAULT_TEMPLATE_ENTRIES)),
        "default_export_stem": f"{FOLDER_LIST_STEM}_{date.today().strftime('%Y%m%d')}",
        **context,
    }, status_code=422 if error else 200)


def _redirect(where: str) -> RedirectResponse:
    return RedirectResponse(where, status_code=303)


@router.get("/browse")
def browse(request: Request, path: str = "~"):
    """사용자가 탐색을 누른 위치의 바로 아래 폴더 이름만 표시한다."""
    t = request.app.state.t
    try:
        folder = _path(path, t)
        if not folder.is_dir() or not _readable(folder, t):
            raise ValueError("열 수 없는 폴더입니다.")
        with os.scandir(folder) as entries:
            children = []
            for entry in entries:
                p = Path(entry.path)
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False) and not entry.is_junction() and _readable(p, t):
                    children.append({"name": entry.name, "path": tilde(p, t.home)})
                if len(children) == 200:
                    break
        parent = tilde(folder.parent, t.home) if folder != folder.parent else None
        return {"path": tilde(folder, t.home), "parent": parent, "children": sorted(children, key=lambda p: p["name"])}
    except (OSError, ValueError) as error:
        return JSONResponse({"error": str(error)}, status_code=422)


_CHOOSE_FOLDER_SCRIPT = """
on run argv
  set promptText to item 1 of argv
  set initPath to item 2 of argv
  set multiFlag to item 3 of argv
  tell application "System Events" to activate
  if initPath is not "" then
    set defaultLoc to POSIX file initPath
    if multiFlag is "1" then
      set picked to choose folder with prompt promptText default location defaultLoc with multiple selections allowed
    else
      set picked to choose folder with prompt promptText default location defaultLoc
    end if
  else
    if multiFlag is "1" then
      set picked to choose folder with prompt promptText with multiple selections allowed
    else
      set picked to choose folder with prompt promptText
    end if
  end if
  if multiFlag is "1" then
    set outLines to {}
    repeat with f in picked
      set end of outLines to POSIX path of f
    end repeat
    set AppleScript's text item delimiters to linefeed
    return outLines as text
  else
    return POSIX path of picked
  end if
end run
""".strip()


def _pick_folders_macos(prompt: str, initial: str, multiple: bool) -> dict:
    """macOS 네이티브 Finder 폴더 선택 창을 연다. 미지원·오류 시 웹 팝업 폴백 신호를 반환한다."""
    if sys.platform != "darwin" or os.environ.get("PYTEST_CURRENT_TEST") or not shutil.which("osascript"):
        return {"fallback": True, "paths": []}
    try:
        proc = subprocess.run(
            ["osascript", "-e", _CHOOSE_FOLDER_SCRIPT, prompt or "폴더를 선택하세요", initial, "1" if multiple else "0"],
            capture_output=True, text=True, timeout=300, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"fallback": True, "paths": []}
    if proc.returncode != 0:
        err = (proc.stderr or "").lower()
        if "-128" in err or "user canceled" in err or "사용자" in err:
            return {"cancelled": True, "paths": []}
        log.warning("[폴더선택] 네이티브 탐색기 호출 실패(%d), 웹 팝업으로 전환", proc.returncode)
        return {"fallback": True, "paths": []}
    raw = [line.rstrip("/") or "/" for line in proc.stdout.splitlines() if line.strip()]
    return {"paths": raw} if raw else {"cancelled": True, "paths": []}


_PICK_LOCK = threading.Lock()  # 폴더 선택 창은 한 번에 하나만 띄운다


def _running_tests() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _is_dir(path: str) -> bool:
    return os.path.isdir(path)


def choose_folders(prompt: str, initial: str, multiple: bool) -> list[str] | None:
    if sys.platform != "win32":
        return None
    from tongdory2.web.windows_folder import choose_folders as _choose_folders

    return _choose_folders(prompt, initial, multiple)


def _pick_folders_windows(prompt: str, initial: str, multiple: bool) -> dict:
    """Windows 탐색기 형식의 폴더 선택 창을 열고, 호출 실패 시 웹 선택으로 전환한다."""
    if sys.platform != "win32" or _running_tests():
        return {"fallback": True, "paths": []}
    try:
        paths = choose_folders(prompt or "폴더를 선택하세요", initial, multiple)
    except OSError as exc:
        log.warning("[폴더선택] Windows 폴더 선택 창 오류, 웹 선택으로 전환: %s", exc)
        return {"fallback": True, "paths": []}
    if paths is None:
        return {"cancelled": True, "paths": []}
    if not paths or not all(_is_dir(path) for path in paths):
        log.warning("[폴더선택] Windows 폴더 선택 창에서 실제 폴더 경로를 받지 못해 웹 선택으로 전환")
        return {"fallback": True, "paths": []}
    return {"paths": paths}


@router.post("/pick-folder")
def pick_folder(request: Request, multiple: bool = Form(False), prompt: str = Form("폴더를 선택하세요"), initial: str = Form("")):
    """OS 기본 폴더 선택 창(macOS Finder, Windows 폴더 선택 창)을 우선 호출하고, 미지원 환경은 웹 팝업으로 폴백."""
    t = request.app.state.t
    init_dir = ""
    if initial.strip():
        try:
            cand = _path(initial, t)
            if cand.is_dir() and _readable(cand, t):
                init_dir = str(cand)
        except ValueError:
            pass
    picker = _pick_folders_windows if sys.platform == "win32" else _pick_folders_macos
    if not _PICK_LOCK.acquire(blocking=False):  # 다른 버튼·탭에서 이미 선택 창이 열려 있다
        return JSONResponse({"cancelled": True, "paths": []})
    try:
        res = picker(prompt.strip()[:120], init_dir, multiple)
    finally:
        _PICK_LOCK.release()
    if res.get("paths"):
        res["paths"] = [tilde(Path(p), t.home) for p in res["paths"]]
    return JSONResponse(res)


@router.get("/{step}", response_class=HTMLResponse)
def page(request: Request, step: str):
    if step not in STEPS:
        return _redirect("/")
    t = request.app.state.t
    pending = pending_setup_step(t.db.list_roots(), SpecStore(t.db.conn).current())
    if pending and tuple(STEPS).index(step) > tuple(STEPS).index(pending):
        return _redirect(f"/setup/{pending}")
    return _render(request, step)


@router.post("/structure/preview")
def preview_structure(
    request: Request,
    reference: str = Form(""),
    document: UploadFile | None = File(None),
    current_paths: str | None = Form(None),
):
    t = request.app.state.t
    uploaded = document is not None and bool(document.filename)
    try:
        if bool(reference.strip()) == uploaded:
            raise ValueError(msg("setup.err.choose_one_source"))
        if uploaded:
            data = document.file.read(MAX_DOCUMENT + 1)
            if len(data) > MAX_DOCUMENT:
                raise ValueError(msg("setup.err.doc_too_large"))
            paths = document_paths(document.filename, data)
        else:
            root = _path(reference, t)
            if root == Path(root.anchor) or root == t.home or not root.is_dir() or not _readable(root, t):
                raise ValueError(msg("setup.err.choose_sub_folder"))
            paths = [(path, False) for path in folder_paths(root, lambda p: _readable(p, t))]
        ex = extraction(paths)
    except (BadZipFile, InvalidFileException, ParseError, XMLSyntaxError, KeyError, OSError, ValueError) as error:
        log.info("[폴더목록] 구조 미리보기 실패 (%s)", type(error).__name__)
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeError) else ""
        base_msg = message or msg("setup.err.preview_read_failed")
        extra: dict = {"reference": reference}
        if current_paths is not None:
            extra["paths"] = current_paths
            extra["preserve_draft"] = True
            if current_paths.strip():
                base_msg += " " + msg("setup.err.kept_editing")
        return _render(request, "structure", error=base_msg, **extra)
    required = [n.display_path for n in ex.nodes if n.required_for_handover]
    display_paths = [n.display_path for n in ex.nodes]
    formatted_paths = format_editor_lines([(n.display_path, n.required_for_handover) for n in ex.nodes])
    source = Path(document.filename.replace("\\", "/")).name if uploaded else tilde(root, t.home)
    return _render(request, "structure", paths=formatted_paths,
                   required=required, preview=True, reference="" if uploaded else source,
                   imported_from=source, imported_kind="file" if uploaded else "folder",
                   imported_count=len(display_paths),
                   tree_rows=folder_tree(set(display_paths), {}, proposed=set(), created=set()),
                   unnumbered=[n.display_path for n in ex.nodes if parse(n.segment).number is None])


@router.post("/structure/export")
def export_structure(
    request: Request,
    paths: str = Form(""),
    required: list[str] = Form(default=[]),
    fmt: str = Form("json"),
    filename: str = Form(""),
    save_dir: str = Form(""),
):
    t = request.app.state.t
    fmt_clean = fmt.strip().lower()
    if fmt_clean not in ("json", "txt"):
        return JSONResponse({"error": msg("setup.err.unsupported_export_fmt"), "code": "setup.err.unsupported_export_fmt"}, status_code=422)
    try:
        entries = parse_editor_text(paths, set(required))
        ex = extraction(entries)
        valid_entries = [(n.display_path, n.required_for_handover) for n in ex.nodes]
        content = export_structure_json(valid_entries) if fmt_clean == "json" else export_structure_txt(valid_entries)
        clean_name = sanitize_filename(filename, fmt_clean, f"{FOLDER_LIST_STEM}_{date.today().strftime('%Y%m%d')}")
        return export_text_or_save(content, clean_name, fmt_clean, save_dir, t)
    except (ValueError, OSError) as error:
        log.info("[폴더목록] 저장 실패 (%s)", type(error).__name__)
        return JSONResponse({"error": str(error)}, status_code=422)


def _changed(t, revision: str) -> bool:
    return bool(revision and revision != scope_key(t.db.list_roots(), SpecStore(t.db.conn).current()))


KEPT = " 방금 입력한 내용은 칸에 그대로 남겨 두었습니다."


def _conflict(request: Request, step: str, **typed):
    return _render(request, step, error="다른 화면에서 설정이 변경되었습니다. 최신 설정과 비교한 뒤 다시 저장해 주세요." + KEPT, **typed)

def _wait_for_job(request: Request, step: str, notice: str, **typed):
    return _render(request, step, error=notice + KEPT, **typed)


@router.post("/structure")
def save_structure(request: Request, paths: str = Form(""), required: list[str] = Form(default=[]),
                   revision: str = Form(""), reference: str = Form("")):
    try:
        ex = extraction(parse_editor_text(paths, set(required)))
    except ValueError as error:
        return _render(request, "structure", error=str(error), paths=paths, required=required, preview=True, reference=reference)
    t = request.app.state.t
    with t.setup_lock:
        if notice := settings_change_notice(t):
            return _wait_for_job(request, "structure", notice, paths=paths)
        if _changed(t, revision):
            return _conflict(request, "structure", paths=paths)
        result = SpecStore(t.db.conn).confirm(ex, {n.node_id for n in ex.nodes}, "build", None, collect_only=True,
                                              structure_reference=reference.strip()[:4096])
    log.info("[폴더목록] 구조 v%d 확정 (%d개)", result["version"], len(ex.nodes))
    return _redirect("/setup/sources")


def _checks(paths, role, roots, t, acknowledged: bool):
    checks = []
    warnings = []
    for path in dict.fromkeys(paths):
        check = check_root(path, role, roots, t.home, t.app_data)
        if check.decision == Decision.REFUSE:
            raise ValueError(f"{tilde(path, t.home)} · {' / '.join(check.messages)}")
        if check.warn_codes and not acknowledged:
            warnings += [(tilde(path, t.home), m) for m in check.messages]  # 조건 하나에 한 줄
        roots.append(RegisteredRoot(str(path), check.realpath, role, True))
        checks.append(check)
    # 경고가 한 폴더에서만 나오면 경로는 빼고 조건만 보인다 (폴더는 바로 위 입력란에 있다)
    one_folder = len({p for p, _ in warnings}) <= 1
    return checks, [m if one_folder else f"{p} · {m}" for p, m in warnings]


@router.post("/sources")
def save_sources(request: Request, paths: str = Form(""), confirm_warnings: bool = Form(False), revision: str = Form("")):
    t = request.app.state.t
    with t.setup_lock:
        if notice := settings_change_notice(t):
            return _wait_for_job(request, "sources", notice, sources=paths)
        if _changed(t, revision):
            return _conflict(request, "sources", sources=paths)
        if not SpecStore(t.db.conn).current():
            return _redirect("/setup/structure")
        try:
            selected = [_path(line, t) for line in paths.splitlines() if line.strip()]
            if not selected:
                raise ValueError("정리 대상 폴더를 한 곳 이상 선택해 주세요.")
            if len(selected) > 30:
                raise ValueError("정리 대상 폴더는 한 번에 30곳까지 선택할 수 있습니다.")
            roots = [r for r in t.db.list_roots() if r.role != Role.COLLECT]
            current = next((r for r in roots if r.role == Role.CURRENT), None)
            if current and any(is_within(p, current.path) or is_within(current.path, p) for p in selected):
                raise ValueError(f"정리 대상 폴더가 기존 저장 폴더({tilde(current.path, t.home)})와 겹칩니다. "
                                 "저장 폴더를 먼저 바꾸거나 다른 정리 대상 폴더를 선택해 주세요.")
            checks, warnings = _checks(selected, Role.COLLECT, roots, t, confirm_warnings)
            if warnings:
                return _render(request, "sources", sources=paths, warnings=warnings)
            t.db.save_setup_roots(Role.COLLECT, checks)
        except ValueError as error:
            return _render(request, "sources", error=str(error), sources=paths)
    log.info("[대상등록] 정리 대상 폴더 %d곳 확정", len(checks))
    return _redirect("/setup/destination")


@router.post("/destination")
def save_destination(request: Request, path: str = Form(""), confirm_warnings: bool = Form(False), revision: str = Form("")):
    t = request.app.state.t
    with t.setup_lock:
        if notice := settings_change_notice(t):
            return _wait_for_job(request, "destination", notice, destination=path)
        if _changed(t, revision):
            return _conflict(request, "destination", destination=path)
        roots = [r for r in t.db.list_roots() if r.role != Role.CURRENT]
        pending = pending_setup_step(roots, SpecStore(t.db.conn).current())
        if pending in ("structure", "sources"):
            return _redirect(f"/setup/{pending}")
        try:
            destination = _path(path, t)
            if any(r.role == Role.COLLECT and (is_within(destination, r.path) or is_within(r.path, destination))
                   for r in roots):
                raise ValueError("정리 대상 폴더와 겹치지 않는 저장 폴더를 선택해 주세요.")
            if any(part.casefold() in SKIP_DIRS or TOP_IDS.get(parse(part).key) == "src"
                   for part in destination.parts):
                raise ValueError("소스·빌드·Git 폴더 안은 저장 폴더로 지정할 수 없습니다.")
            if not os.access(destination, os.W_OK | os.X_OK):
                raise ValueError("저장 폴더가 없거나 쓰기 권한이 없습니다.")
            checks, warnings = _checks([destination], Role.CURRENT, roots, t, confirm_warnings)
            if warnings:
                return _render(request, "destination", destination=path, warnings=warnings)
            storage = None
            storage_root = next((r for r in roots if r.role == Role.STORAGE), None)
            if storage_root is None:
                candidates = [t.home / "Documents" / STORAGE_DEFAULT_NAME, t.home / STORAGE_DEFAULT_NAME]
                storage = next((check for p in candidates
                                if (check := check_root(p, Role.STORAGE, roots, t.home, t.app_data)).decision == Decision.OK), None)
                if storage is None:
                    raise ValueError("관리용 보관함을 만들 수 없습니다. 쓰기 가능한 저장 폴더를 선택해 주세요.")
                storage_root = RegisteredRoot("storage", storage.realpath, Role.STORAGE, True)
                roots.append(storage_root)
            elif not storage_root.confirmed:
                storage_path = _path(str(storage_root.path), t)
                roots.remove(storage_root)
                validated, warnings = _checks([storage_path], Role.STORAGE, roots, t, confirm_warnings)
                if warnings:
                    return _render(request, "destination", destination=path, warnings=warnings)
                storage = validated[0]
                storage_root = roots[-1]
            ensure_layout(storage_root, Scope(roots, t.home, t.app_data))
            t.db.save_setup_roots(Role.CURRENT, checks, storage)
        except (OSError, ValueError) as error:
            return _render(request, "destination", error=str(error), destination=path)
    log.info("[대상등록] 저장 폴더 확정")
    return _redirect("/")
