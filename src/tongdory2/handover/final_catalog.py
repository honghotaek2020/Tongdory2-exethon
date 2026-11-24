"""최종 결과 목록표(MD·JSON·TXT) 생성 — 완료 화면(/s3?done=1)과 인수인계(/s4) 공용.

원칙:
- 읽기 전용으로 현재 저장 폴더와 확정 목록을 대조한다.
- 민감(L3) 파일은 저장된 판정과 앞부분(head) 재검사를 모두 적용해 파일명·경로를 아예 넣지 않는다.
- JSON·TXT 출력은 1단계 기준 폴더 목록 스키마(tongdory-folder-list v1, 최대 1,000개 노드)와 100% 호환된다.
"""

from __future__ import annotations

import json
import os
import stat
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from tongdory2.fsx.paths import nfc, real, tilde
from tongdory2.fsx.scan import SKIP_DIRS
from tongdory2.fsx.scope import Access, Scope
from tongdory2.fsx.sensitivity import classify_sensitivity, mask, read_head
from tongdory2.handover.checklist import DRAFT, STATUS_LABEL
from tongdory2.spec.normalize import TOP_IDS, parse
from tongdory2.spec.structure_input import (
    MAX_DEPTH,
    MAX_NODES,
    STRUCTURE_JSON_FORMAT,
    STRUCTURE_JSON_VERSION,
    allowed_segment,
)

MAX_FILES_PER_FOLDER_MD = 30


def folder_presence(folder: Path, scope: Scope) -> str:
    """실제 폴더와 미생성 기준 경로를 구분한다. 접근 불가는 없음으로 단정하지 않는다."""
    if not scope.check(folder, Access.READ).allowed:
        return "unavailable"
    try:
        return "exists" if stat.S_ISDIR(folder.stat().st_mode) else "not_directory"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unavailable"


def batch_folder_results(ops: list[dict], root: Path, scope: Scope) -> list[dict]:
    """선택한 작업의 유지 중인 이동·실제 생성만 표시한다. 기존 파일은 섞지 않는다."""
    rows: dict[str, dict] = {}
    for op in ops:
        if op.get("status") != "DONE" or op.get("undo_status") == "UNDONE":
            continue
        if op.get("op") == "MOVE":
            folder = Path(op["dst"]).parent
        elif op.get("op") == "MKDIR" and op.get("created_dir"):
            folder = Path(op["dst"])
        else:
            continue
        if not folder.is_relative_to(root):
            continue
        rel = folder.relative_to(root).as_posix()
        if ".." in folder.parts or not _safe_label(rel):
            continue
        row = rows.setdefault(rel, {"path": rel, "created": False, "moved": 0,
                                    "files": [], "unlisted": 0})
        if op["op"] == "MKDIR":
            row["created"] = True
        else:
            row["moved"] += 1
            dst = Path(op["dst"])
            if len(row["files"]) < 4 and not _is_l3_file(dst, scope, set()):
                row["files"].append(nfc(dst.name))
    for row in rows.values():
        row["presence"] = folder_presence(root / row["path"], scope)
        row["unlisted"] = row["moved"] - len(row["files"])
    return [rows[path] for path in sorted(rows, key=lambda p: p.split("/"))]


@dataclass
class CatalogFolder:
    path: str
    required: bool
    status: str  # PRESENT | DRAFT_ONLY | PRESENT_IN_PLACE | MISSING
    is_new: bool = False
    files: list[str] = field(default_factory=list)
    file_count: int = 0
    in_place: list[str] = field(default_factory=list)
    moved_in_batch: int = 0
    folder_exists: bool = False
    excluded_count: int = 0


@dataclass
class FinalCatalog:
    project: str
    root_display: str
    exported_at: str
    exported_display: str
    folders: list[CatalogFolder]
    all_folder_paths: list[str]
    total_folders: int
    new_folders: int
    total_files: int
    moved_count: int
    created_dirs: int
    left_count: int
    hold_count: int
    excluded_sensitive: int
    required_total: int
    required_ok: int
    missing_required: list[str]
    batch_id: str | None = None
    batch_name: str | None = None
    batch_status: str | None = None
    is_undone: bool = False
    is_partial_undo: bool = False
    undone_count: int = 0
    is_review_only: bool = False
    has_unresolved: bool = False
    truncated_folders: int = 0


def _safe_label(value: str) -> bool:
    return all(classify_sensitivity(Path(part)).level != "L3" for part in Path(value).parts)


def _is_l3_name_or_known(path: Path, sensitive_paths: set[str]) -> bool:
    if path.is_symlink() or not _safe_label(path.name):
        return True
    try:
        if sensitive_paths and str(real(path)) in sensitive_paths:
            return True
    except OSError:
        return True
    return classify_sensitivity(path).level == "L3"


def _is_l3_content(path: Path, scope: Scope) -> bool:
    head = read_head(path, scope)
    if head is None:
        return True
    return classify_sensitivity(path, head).level == "L3"


def _is_l3_file(path: Path, scope: Scope, sensitive_paths: set[str]) -> bool:
    if _is_l3_name_or_known(path, sensitive_paths):
        return True
    return _is_l3_content(path, scope)


def _scan_dir_files(
    folder: Path,
    scope: Scope,
    sensitive_paths: set[str],
    *,
    display_limit: int | None = None,
) -> tuple[list[str], int, int]:
    """폴더 내 비민감 파일 목록, 파일 수, 제외된 민감 파일 수를 반환한다.

    display_limit가 주어지면(예: 완료 화면 표 렌더링용 4개), 화면에 노출할 최대 개수까지만
    앞부분(read_head)을 확인해 대량 파일 폴더에서 불필요한 디스크 읽기를 막는다.
    """
    if not folder.is_dir() or not scope.check(folder, Access.READ).allowed:
        return [], 0, 0
    files: list[str] = []
    file_count = 0
    excluded = 0
    try:
        entries = sorted(os.scandir(folder), key=lambda e: nfc(e.name))
    except OSError:
        return [], 0, 0
    for entry in entries:
        if entry.name.startswith("."):
            continue
        p = Path(entry.path)
        if not entry.is_file(follow_symlinks=False):
            continue
        if _is_l3_name_or_known(p, sensitive_paths):
            excluded += 1
            continue
        if display_limit is None or len(files) < display_limit:
            if _is_l3_content(p, scope):
                excluded += 1
                continue
            files.append(nfc(entry.name))
            file_count += 1
        else:
            file_count += 1
    return files, file_count, excluded


def _discover_disk_dirs(current_root: Path, scope: Scope) -> list[str]:
    if not current_root.is_dir() or not scope.check(current_root, Access.READ).allowed:
        return []
    found: list[str] = []

    def visit(directory: Path, depth: int) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda e: nfc(e.name))
        except OSError:
            return
        for entry in children:
            name = nfc(entry.name)
            if name.startswith(".") or name.casefold() in SKIP_DIRS or TOP_IDS.get(parse(name).key) == "src":
                continue
            p = Path(entry.path)
            if not entry.is_dir(follow_symlinks=False) or not scope.check(p, Access.READ).allowed:
                continue
            rel = nfc(p.relative_to(current_root).as_posix())
            if _safe_label(rel):
                found.append(rel)
            if depth < MAX_DEPTH:
                visit(p, depth + 1)

    visit(current_root, 1)
    return found


def _select_reusable_folders(rows: list[CatalogFolder]) -> tuple[list[CatalogFolder], int]:
    """1단계 기준 목록(최대 MAX_NODES=1,000개 노드)으로 항상 다시 불러올 수 있게 선별한다."""
    valid_rows = [
        r for r in rows
        if (parts := r.path.split("/")) and len(parts) <= MAX_DEPTH and all(allowed_segment(part) for part in parts)
    ]
    prioritized = sorted(
        valid_rows,
        key=lambda r: (not r.required, r.is_new, r.file_count == 0, r.path),
    )
    expanded: set[str] = set()
    kept_paths: set[str] = set()
    omitted = 0
    for r in prioritized:
        parts = r.path.split("/")
        prefixes = {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}
        if len(expanded | prefixes) <= MAX_NODES:
            expanded |= prefixes
            kept_paths.add(r.path)
        else:
            omitted += 1
    kept = [r for r in valid_rows if r.path in kept_paths]
    return kept, omitted


def build_final_catalog(
    spec: dict,
    current_root: Path,
    home: Path,
    scope: Scope,
    *,
    inplace_entries: list[dict] | None = None,
    sensitive_paths: set[str] | None = None,
    selected_batch: dict | None = None,
    ops: list[dict] | None = None,
    hold_count: int = 0,
    left_count: int = 0,
    display_limit: int | None = None,
    now: datetime | None = None,
) -> FinalCatalog:
    now = now or datetime.now().astimezone()
    known_sensitive = sensitive_paths or set()
    inplace_entries = inplace_entries or []
    ops = ops or []

    nodes = [
        n for n in spec["body"]["nodes"]
        if n["status"] == "ADOPTED" and n["role"] == "DEST"
        and n.get("profile", "common") in ("common", "build") and n["node_id"] != "src"
    ]
    spec_by_path: dict[str, dict] = {}
    node_id_to_path: dict[str, str] = {}
    for n in nodes:
        p = nfc(n["display_path"])
        if _safe_label(p):
            spec_by_path[p] = n
            node_id_to_path[n["node_id"]] = p

    # 배치에서 실제 유지 중인 이동/생성 및 되돌린 내역 집계(부분 되돌리기 반영)
    moved_by_folder: Counter[str] = Counter()
    created_set: set[str] = set()
    active_moved = 0
    active_created_dirs = 0
    undone_count = 0
    for op in ops:
        if op.get("op") in ("MOVE", "TRASH") and op.get("undo_status") == "UNDONE":
            undone_count += 1
            continue
        if op.get("status") != "DONE" or op.get("undo_status") == "UNDONE":
            continue
        dst = Path(op["dst"])
        if op.get("op") in ("MOVE", "TRASH"):
            active_moved += 1
            if dst.parent.is_relative_to(current_root):
                rel = nfc(dst.parent.relative_to(current_root).as_posix())
                if rel != ".":
                    moved_by_folder[rel] += 1
        elif op.get("op") == "MKDIR" and op.get("created_dir"):
            active_created_dirs += 1
            if dst.is_relative_to(current_root):
                rel = nfc(dst.relative_to(current_root).as_posix())
                if rel != ".":
                    created_set.add(rel)

    # 제자리 목록화(AI 협업 문서) 매핑 — 민감 파일 엄격 제외
    ip_by_path: dict[str, list[str]] = {}
    excluded_sensitive = 0
    for e in inplace_entries:
        if e.get("reason_code") == "SENSITIVE" or e.get("sensitivity") == "L3":
            excluded_sensitive += 1
            continue
        if not e.get("node_id") or e.get("verdict") != "JOIN" or e.get("backup_of"):
            continue
        p = Path(e["path"])
        if _is_l3_file(p, scope, known_sensitive):
            excluded_sensitive += 1
            continue
        target_path = node_id_to_path.get(e["node_id"])
        if not target_path:
            continue
        label = e.get("display") or e.get("title") or e["name"]
        if _safe_label(label):
            ip_by_path.setdefault(target_path, []).append(nfc(label))

    # 실제 저장 폴더의 디렉터리 + 확정 명세 폴더 통합
    disk_dirs = _discover_disk_dirs(current_root, scope)
    all_path_set = set(spec_by_path) | set(disk_dirs) | created_set
    expanded: set[str] = set()
    for p in all_path_set:
        parts = p.split("/")
        for i in range(1, len(parts) + 1):
            expanded.add("/".join(parts[:i]))
    all_paths = sorted(expanded)

    parents_with_child: set[str] = set()
    for p in all_paths:
        parts = p.split("/")
        for i in range(1, len(parts)):
            parents_with_child.add("/".join(parts[:i]))

    rows: list[CatalogFolder] = []
    total_files = 0
    for p in all_paths:
        files, fcount, exc = _scan_dir_files(
            current_root / p, scope, known_sensitive, display_limit=display_limit
        )
        excluded_sensitive += exc
        total_files += fcount
        ips = ip_by_path.get(p, [])
        node = spec_by_path.get(p)
        required = bool(node.get("required_for_handover")) if node else False
        is_leaf = p not in parents_with_child
        is_new = p in created_set or (p not in spec_by_path and (fcount > 0 or is_leaf))
        if not (is_leaf or required or fcount > 0 or ips):
            continue
        if fcount > 0:
            status = "DRAFT_ONLY" if (files and fcount == len(files) and all(DRAFT.search(f) for f in files)) else "PRESENT"
        elif ips:
            status = "PRESENT_IN_PLACE"
        else:
            status = "MISSING"
        rows.append(
            CatalogFolder(
                path=p,
                required=required,
                status=status,
                is_new=is_new,
                files=files,
                file_count=fcount,
                in_place=ips,
                moved_in_batch=moved_by_folder.get(p, 0),
                folder_exists=(current_root / p).is_dir(),
                excluded_count=exc,
            )
        )

    req_rows = [r for r in rows if r.required]
    req_ok = sum(1 for r in req_rows if r.status == "PRESENT")
    missing_req = [r.path for r in req_rows if r.status in ("MISSING", "DRAFT_ONLY")]

    summary = (selected_batch or {}).get("summary") or {}
    batch_status = (selected_batch or {}).get("status")
    is_undone = batch_status == "UNDONE"
    is_partial_undo = batch_status == "PARTIAL_UNDO"
    is_review_only = bool(summary.get("review_only"))
    has_unresolved = bool((summary.get("denied") or 0) + (summary.get("failed") or 0)
                          or any(op.get("status") in ("FAILED", "DENY", "DENIED") for op in ops))

    if selected_batch is None or is_undone:
        moved_count = 0
        created_dirs = 0
    elif ops:
        moved_count = active_moved
        created_dirs = active_created_dirs
    else:
        moved_count = int(summary.get("moved") or 0)
        created_dirs = int(summary.get("created_dirs") or 0)

    new_folders = sum(1 for r in rows if r.is_new)
    _, truncated_folders = _select_reusable_folders(rows)

    return FinalCatalog(
        project=nfc(current_root.name) if _safe_label(current_root.name) else "과제폴더",
        root_display=tilde(current_root, home),
        exported_at=now.isoformat(timespec="seconds"),
        exported_display=now.strftime("%Y-%m-%d %H:%M"),
        folders=rows,
        all_folder_paths=all_paths,
        total_folders=len(all_paths),
        new_folders=new_folders,
        total_files=total_files,
        moved_count=moved_count,
        created_dirs=created_dirs,
        left_count=max(int(left_count), 0),
        hold_count=max(int(hold_count), 0),
        excluded_sensitive=excluded_sensitive,
        required_total=len(req_rows),
        required_ok=req_ok,
        missing_required=missing_req,
        batch_id=(selected_batch or {}).get("batch_id"),
        batch_name=(selected_batch or {}).get("name"),
        batch_status=batch_status,
        is_undone=is_undone,
        is_partial_undo=is_partial_undo,
        undone_count=undone_count,
        is_review_only=is_review_only,
        has_unresolved=has_unresolved,
        truncated_folders=truncated_folders,
    )


def _md_escape(text: str) -> str:
    return " ".join(text.splitlines()).replace("\\", "\\\\").replace("`", "\\`")


def render_final_md(catalog: FinalCatalog) -> str:
    lines = [
        "# 개발 자료 최종 목록표 (인수인계용)",
        "",
        f"> 과제 폴더: {_md_escape(mask(catalog.project))}",
        f"> 저장 시각: {catalog.exported_display}",
        f"> 정리 결과: 전체 폴더 {catalog.total_folders}개 (새로 만든 폴더 {catalog.new_folders}개) · "
        f"확인된 파일 {catalog.total_files}개 · 필수 산출물 {catalog.required_ok}/{catalog.required_total}",
        "> 보안 안내: 민감(L3)으로 분류된 파일은 목록표에 포함하지 않았습니다.",
    ]
    if catalog.is_undone:
        lines.append("> 참고: 선택한 실행 기록은 이미 되돌려진 상태이며, 현재 저장 폴더의 최신 모습으로 작성했습니다.")
    elif catalog.is_partial_undo:
        lines.append(
            f"> 참고: 선택한 실행 기록은 파일 {catalog.undone_count}개가 되돌려진 상태이며, "
            "현재 유지 중인 이동 결과와 저장 폴더 최신 모습으로 작성했습니다."
        )
    lines += ["", "## 폴더별 자료 목록", ""]

    by_path = {row.path: row for row in catalog.folders}
    for full_path in catalog.all_folder_paths:
        parts = full_path.split("/")
        depth = len(parts) - 1
        indent = "  " * depth
        name = _md_escape(parts[-1])
        row = by_path.get(full_path)
        if not row:
            lines.append(f"{indent}- {name}")
            continue
        badges: list[str] = []
        if row.required:
            if row.status == "MISSING":
                badges.append("필수 · 없음 — 확인 필요")
            else:
                badges.append(f"필수 · {STATUS_LABEL[row.status]}")
        elif row.is_new:
            badges.append("새로 만든 폴더")
        elif row.status != "MISSING":
            badges.append(STATUS_LABEL[row.status])
        badge_text = f" `[{' / '.join(badges)}]`" if badges else ""
        count_text = f" ({row.file_count}개)" if row.file_count else ""
        lines.append(f"{indent}- {name}{badge_text}{count_text}")

        file_indent = "  " * (depth + 1)
        shown = row.files[:MAX_FILES_PER_FOLDER_MD]
        for fname in shown:
            lines.append(f"{file_indent}- {_md_escape(mask(fname))}")
        if row.file_count > len(shown):
            lines.append(f"{file_indent}- 외 {row.file_count - len(shown)}개")
        for ip_doc in row.in_place[:10]:
            lines.append(f"{file_indent}- (제자리 보관) {_md_escape(mask(ip_doc))}")
        if len(row.in_place) > 10:
            lines.append(f"{file_indent}- (제자리 보관) 외 {len(row.in_place) - 10}개")

    lines += ["", "## 인수인계 확인 사항", ""]
    if catalog.missing_required:
        joined = ", ".join(f"`{_md_escape(p)}`" for p in catalog.missing_required)
        lines.append(f"- 비어 있거나 초안만 있는 필수 산출물: {joined}")
    else:
        lines.append("- 필수 산출물이 모두 확인되었습니다.")
    if catalog.moved_count:
        lines.append(f"- 이번 작업에서 유지 중인 이동 파일: {catalog.moved_count}개 (새로 만든 폴더 {catalog.created_dirs}개)")
    if catalog.hold_count:
        lines.append(f"- 판단 보류 파일: {catalog.hold_count}개 (원래 위치에서 직접 확인 필요)")
    if catalog.left_count:
        lines.append(f"- 원래 자리에 남겨 둔 파일: {catalog.left_count}개")
    if catalog.excluded_sensitive:
        lines.append(f"- 민감(L3) 파일 {catalog.excluded_sensitive}개는 보안을 위해 목록표에서 제외했습니다.")
    return "\n".join(lines) + "\n"


def render_final_json(catalog: FinalCatalog) -> str:
    """1단계 기준 폴더 목록(.json, 최대 1,000개 노드)과 100% 호환되는 최종 목록표 JSON을 만든다."""
    kept, omitted = _select_reusable_folders(catalog.folders)
    folders = [
        {
            "path": row.path,
            "required": row.required,
            "file_count": row.file_count,
        }
        for row in kept
    ]
    payload: dict = {
        "format": STRUCTURE_JSON_FORMAT,
        "version": STRUCTURE_JSON_VERSION,
        "exported_from": "final-result",
        "project": mask(catalog.project),
        "exported_at": catalog.exported_at,
        "folders": folders,
    }
    if omitted:
        payload["truncated_folders"] = omitted
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render_final_txt(catalog: FinalCatalog) -> str:
    """사람이 메모장으로 보기도 좋고 1단계에서 바로 다시 불러올 수도 있는 UTF-8 텍스트 목록."""
    kept, omitted = _select_reusable_folders(catalog.folders)
    lines = [
        f"# 개발 자료 최종 목록표 — {mask(catalog.project)}",
        f"# 저장 시각: {catalog.exported_display} · 전체 폴더 {catalog.total_folders}개 · "
        f"확인된 파일 {catalog.total_files}개 · 필수 {catalog.required_ok}/{catalog.required_total}",
        "# 민감(L3) 파일은 목록표에 포함하지 않았습니다.",
    ]
    if omitted:
        lines.append(f"# 안내: 폴더가 1,000개를 넘어 기준 목록과 파일이 있는 폴더 우선 1,000개까지 담았습니다(생략 {omitted}개).")
    lines.append("")
    for row in kept:
        lines.append(f"{row.path} [필수]" if row.required else row.path)
    return "\n".join(lines) + "\n"
