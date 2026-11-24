"""기존 폴더 또는 정의서에서 목적지와 독립적인 구조 명세를 만들고 저장한다."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from tongdory2.fsx.paths import nfc
from tongdory2.fsx.scan import SKIP_DIRS
from tongdory2.messages import msg
from tongdory2.spec.defaults import describe
from tongdory2.spec.doc_standard import ENTRY_PHASES, read_phase_rows
from tongdory2.spec.extract_standard import Extraction, SpecNode
from tongdory2.spec.normalize import TOP_IDS, compare_key, is_messy, parse

MAX_DOCUMENT = 8 * 1024 * 1024
MAX_NODES = 1000
MAX_DEPTH = 8
STRUCTURE_JSON_FORMAT = "tongdory-folder-list"
STRUCTURE_JSON_VERSION = 1
_BAD = re.compile(r'[\\:*?"<>|\x00-\x1f]')
_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_REQ_SUFFIX = re.compile(r"\s*`?\[필수\]`?\s*$")
_BULLET_PREFIX = re.compile(r"^[-*+]\s+")
_HR_LINE = re.compile(r"^([-*_])\1{2,}\s*$")

DEFAULT_TEMPLATE_ENTRIES: tuple[tuple[str, bool], ...] = (
    ("01.착수/01.과제수행계획서", True),
    ("01.착수/02.착수보고서", False),
    ("02.분석/01.요구사항정의서", True),
    ("02.분석/02.현행분석/01.외부연계현황", False),
    ("03.설계/01.화면설계서", True),
    ("03.설계/02.인터페이스설계서", True),
    ("04.구현/01.개발가이드", False),
    ("05.테스트/01.테스트시나리오", True),
    ("05.테스트/02.테스트결과서", True),
    ("06.이행/01.이행계획서", True),
    ("07.운영/01.운영매뉴얼", True),
    ("08.보고/01.주간보고", False),
    ("08.보고/02.회의록", False),
)


def allowed_segment(name: str) -> bool:
    return (
        bool(name) and not name.startswith(".") and not _BAD.search(name)
        and name.casefold() not in SKIP_DIRS and TOP_IDS.get(parse(name).key) != "src"
        and not is_messy(name)
    )


def normalize_line(raw: str) -> tuple[str, bool]:
    text = raw.lstrip("\ufeff").strip()
    required = False
    if _REQ_SUFFIX.search(text):
        required = True
        text = _REQ_SUFFIX.sub("", text).strip()
    text = text.strip("`").strip()
    while text.endswith("/"):
        text = text[:-1].rstrip()
    if not text:
        return "", required
    parts = [nfc(part.strip()) for part in text.split("/")]
    return "/".join(parts), required


def compact_entries(entries: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """순서를 유지해 중복을 합치고, 자식이 있는 일반 상위 폴더 줄은 생략한다([필수] 상위 폴더는 유지)."""
    ordered: dict[str, bool] = {}
    for raw, req in entries:
        path, inline_req = normalize_line(raw)
        if not path:
            continue
        ordered[path] = ordered.get(path, False) or bool(req) or inline_req
    all_paths = set(ordered)
    parents_with_child: set[str] = set()
    for path in all_paths:
        parts = path.split("/")
        for i in range(1, len(parts)):
            parents_with_child.add("/".join(parts[:i]))
    return [(path, req) for path, req in ordered.items() if path not in parents_with_child or req]


def format_editor_lines(entries: list[tuple[str, bool]]) -> str:
    return "\n".join(f"{path} [필수]" if req else path for path, req in compact_entries(entries))


def parse_editor_text(text: str, required_paths: set[str] | None = None) -> list[tuple[str, bool]]:
    norm_required = {normalize_line(item)[0] for item in (required_paths or set()) if item.strip()}
    result: list[tuple[str, bool]] = []
    seen: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        path, inline_req = normalize_line(line)
        if not path:
            raise ValueError(msg("setup.err.invalid_line", lineno=lineno, raw=line.strip()[:100]))
        if path in seen:
            raise ValueError(msg("setup.err.duplicate_line", lineno=lineno, first=seen[path], path=path))
        seen[path] = lineno
        result.append((path, inline_req or path in norm_required))
    return result


def export_structure_txt(entries: list[tuple[str, bool]]) -> str:
    body = format_editor_lines(entries)
    return (body + "\n") if body else ""


def export_structure_json(entries: list[tuple[str, bool]]) -> str:
    payload = {
        "format": STRUCTURE_JSON_FORMAT,
        "version": STRUCTURE_JSON_VERSION,
        "folders": [{"path": path, "required": req} for path, req in compact_entries(entries)],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def sanitize_filename(name: str, ext: str, default_stem: str) -> str:
    clean_ext = ext.lstrip(".").lower()
    raw = nfc((name or "").strip())
    raw = Path(raw.replace("\\", "/")).name
    raw = _FILENAME_BAD.sub("_", raw).strip(" .")
    for suffix in (f".{clean_ext}", ".json", ".txt", ".md"):
        if raw.lower().endswith(suffix):
            raw = raw[: -len(suffix)].strip(" .")
            break
    if not raw or raw in (".", ".."):
        raw = default_stem
    raw = raw[:120].rstrip(" .") or default_stem
    return f"{raw}.{clean_ext}"


def save_file_no_overwrite(folder: Path, filename: str, content: str) -> Path:
    """기존 파일을 덮어쓰지 않고 원자적으로 새 파일을 만든다(같은 이름이 있으면 _1, _2를 붙임)."""
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    encoded = content.encode("utf-8")
    for index in range(0, 1000):
        candidate = folder / (filename if index == 0 else f"{stem}_{index}{suffix}")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
        except Exception:
            try:
                candidate.unlink()
            except OSError:
                pass
            raise
        return candidate
    raise OSError(msg("export.err.too_many_collisions"))


def folder_paths(root: Path, allow) -> list[str]:
    paths: list[str] = []

    def visit(current: Path, depth: int) -> None:
        for child in sorted(current.iterdir()):
            if not allowed_segment(child.name) or child.is_symlink() or not allow(child) or not child.is_dir():
                continue
            paths.append(nfc(child.relative_to(root).as_posix()))
            if len(paths) > MAX_NODES:
                raise ValueError(msg("setup.err.folder_over_limit"))
            if depth < MAX_DEPTH:
                visit(child, depth + 1)

    visit(root, 1)
    return paths


def _table_paths(header: tuple, rows) -> list[tuple[str, bool]] | None:
    names = [str(cell or "").strip() for cell in header]
    path_col = next((i for i, name in enumerate(names) if name in {"폴더 경로", "상대 경로", "경로"}), None)
    if path_col is None:
        phase_rows = read_phase_rows(header, rows)
        if phase_rows is None:
            return None
        return [(f"{phase}/{output}", required) for phase, output, required in phase_rows
                if compare_key(phase) not in ENTRY_PHASES]

    def value(row, index: int) -> str:
        return str(row[index] or "").strip() if len(row) > index else ""

    req_col = next((i for i, name in enumerate(names) if name == "필수"), None)
    return [(value(row, path_col), req_col is not None and value(row, req_col) in {"필수", "예", "Y"})
            for row in rows if value(row, path_col)]


def _xlsx(data: bytes) -> list[tuple[str, bool]]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            rows = iter(sheet.values)
            for header in rows:
                found = _table_paths(header, rows)
                if found is not None:
                    return found
        return []
    finally:
        workbook.close()


def _docx(data: bytes) -> list[tuple[str, bool]]:
    from docx import Document

    doc = Document(io.BytesIO(data))
    for table in doc.tables:
        rows = [tuple(cell.text for cell in row.cells) for row in table.rows]
        if rows and (paths := _table_paths(rows[0], rows[1:])) is not None:
            return paths
    return parse_txt_structure("\n".join(p.text for p in doc.paragraphs))


def parse_txt_structure(text: str) -> list[tuple[str, bool]]:
    entries: list[tuple[str, bool]] = []
    seen: dict[str, int] = {}
    in_fence = False
    for lineno, raw in enumerate(text.lstrip("\ufeff").splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith(("#", ">", "|")) or _HR_LINE.match(stripped):
            continue
        cleaned = _BULLET_PREFIX.sub("", stripped)
        path, req = normalize_line(cleaned)
        if path:
            if path in seen:
                raise ValueError(msg("setup.err.duplicate_line", lineno=lineno, first=seen[path], path=path))
            seen[path] = lineno
            entries.append((path, req))
    return entries


def parse_md_structure(text: str) -> list[tuple[str, bool]]:
    """계층형 Markdown 목록(2칸·4칸 들여쓰기 자동 인식)과 일반 슬래시 경로를 모두 읽는다."""
    raw_entries: list[tuple[str, bool]] = []
    seen: dict[str, int] = {}
    stack: list[str] = []
    indent_unit: int | None = None
    in_fence = False
    for lineno, raw in enumerate(text.lstrip("\ufeff").splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith(("#", ">", "|")) or _HR_LINE.match(stripped):
            continue
        leading = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in leading:
            raise ValueError(msg("setup.err.md_tab", lineno=lineno))
        indent = len(leading)
        is_bullet = bool(_BULLET_PREFIX.match(stripped))
        if is_bullet:
            item_raw = _BULLET_PREFIX.sub("", stripped)
            if indent == 0:
                depth = 0
            else:
                if not stack:
                    raise ValueError(msg("setup.err.md_orphan", lineno=lineno))
                if indent_unit is None:
                    if indent not in (2, 4):
                        raise ValueError(msg("setup.err.md_unit", lineno=lineno))
                    indent_unit = indent
                if indent % indent_unit != 0:
                    raise ValueError(msg("setup.err.md_uneven", lineno=lineno, unit=indent_unit))
                depth = indent // indent_unit
                if depth > len(stack):
                    raise ValueError(msg("setup.err.md_skip_level", lineno=lineno))
            item_path, req = normalize_line(item_raw)
            if not item_path:
                continue
            if depth == 0:
                stack = [item_path]
                full_path = item_path
            else:
                stack = stack[:depth] + [item_path]
                full_path = "/".join(stack)
            if full_path in seen:
                raise ValueError(msg("setup.err.md_duplicate", lineno=lineno, first=seen[full_path], path=full_path))
            seen[full_path] = lineno
            raw_entries.append((full_path, req))
        else:
            if indent > 0:
                raise ValueError(msg("setup.err.md_bullet_required", lineno=lineno))
            stack = []
            path, req = normalize_line(stripped)
            if path:
                if path in seen:
                    raise ValueError(msg("setup.err.md_duplicate", lineno=lineno, first=seen[path], path=path))
                seen[path] = lineno
                raw_entries.append((path, req))
    return compact_entries(raw_entries)


def parse_json_structure(data: bytes) -> list[tuple[str, bool]]:
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(msg("setup.err.json_parse")) from exc
    if not isinstance(obj, dict):
        raise ValueError(msg("setup.err.json_not_object"))
    if obj.get("format") != STRUCTURE_JSON_FORMAT or obj.get("version") != STRUCTURE_JSON_VERSION:
        raise ValueError(msg("setup.err.json_bad_header"))
    folders = obj.get("folders")
    if not isinstance(folders, list) or not folders:
        raise ValueError(msg("setup.err.json_empty"))
    entries: list[tuple[str, bool]] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(folders, start=1):
        if not isinstance(item, dict):
            raise ValueError(msg("setup.err.json_bad_item", index=index))
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(msg("setup.err.json_empty_path", index=index))
        req = item.get("required", False)
        if not isinstance(req, bool):
            raise ValueError(msg("setup.err.json_bad_required", index=index))
        path, inline_req = normalize_line(raw_path)
        if path in seen:
            raise ValueError(msg("setup.err.json_duplicate", index=index, first=seen[path], path=path))
        seen[path] = index
        entries.append((path, req or inline_req))
    return entries


def document_paths(name: str, data: bytes) -> list[tuple[str, bool]]:
    suffix = Path(name).suffix.lower()
    if suffix in (".xlsx", ".docx"):
        with ZipFile(io.BytesIO(data)) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > 32 * 1024 * 1024:
                raise ValueError(msg("setup.err.doc_zip_too_large"))
        return _xlsx(data) if suffix == ".xlsx" else _docx(data)
    if suffix == ".json":
        return parse_json_structure(data)
    if suffix == ".md":
        return parse_md_structure(data.decode("utf-8-sig"))
    if suffix == ".txt":
        return parse_txt_structure(data.decode("utf-8-sig"))
    raise ValueError(msg("setup.err.unsupported_doc_ext"))


def extraction(paths: list[tuple[str, bool]], origin: str = "USER_ADDED") -> Extraction:
    records: dict[str, bool] = {}
    seen_inputs: set[str] = set()
    for raw, required in paths:
        path, inline_req = normalize_line(raw)
        is_req = bool(required) or inline_req
        parts = path.split("/") if path else []
        if not path or len(path) > 240 or len(parts) > MAX_DEPTH or not all(map(allowed_segment, parts)):
            raise ValueError(msg("setup.err.invalid_path", raw=raw[:100]))
        if path in seen_inputs:
            raise ValueError(msg("setup.err.duplicate_path", path=path))
        seen_inputs.add(path)
        for i in range(1, len(parts) + 1):
            parent = "/".join(parts[:i])
            records[parent] = records.get(parent, False) or (i == len(parts) and is_req)
        if len(records) > MAX_NODES:
            raise ValueError(msg("setup.err.max_nodes"))
    if not records:
        raise ValueError(msg("setup.err.empty_list"))

    def node_id(path: str) -> str:
        return "u" + hashlib.sha256(path.encode()).hexdigest()[:16]

    nodes = []
    for path, required in records.items():
        item = PurePosixPath(path)
        label = parse(item.name).name or item.name
        key = parse(item.name).key
        nodes.append(SpecNode(
            node_id(path), node_id(item.parent.as_posix()) if len(item.parts) > 1 else None,
            "common", item.name, path, label, key, describe(key, label, []),
            "DEST", "ADOPTED", 0, 0, [], [], origin=origin, required_for_handover=required,
        ))
    return Extraction([], {}, nodes, [], {})
