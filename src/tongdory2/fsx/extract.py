"""로컬 텍스트 추출 + 사람용 미리보기 (02_정책 §3.3, 결정 #13·#41, TC M-08).

- `text`  = 등급별로 **보내도 되는 만큼만**: L1 마스킹 본문 ≤ max_chars, L2 마스킹 제목 ≤80자(로그는 빈 값),
            L3 빈 값.
- `preview` = 로컬 뷰어 전용(외부 전송 금지) 앞부분 줄들(~40줄). L3는 안내 문구 1줄만.
- 파일은 링크 미추적(O_NOFOLLOW)으로 메모리에 읽은 뒤 라이브러리에 넘긴다. 손상 파일에도 예외를 내지 않는다.
"""

from __future__ import annotations

import io
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from tongdory2.fsx.sensitivity import IMAGE_EXTS, mask

if TYPE_CHECKING:
    from tongdory2.fsx.scope import Scope

L3_NOTICE = "민감 — 내용 표시 안 함"
TITLE_MAX = 80
PREVIEW_LINES = 40
PREVIEW_LINE_CHARS = 200
XLSX_PREVIEW_ROWS = 15
XLSX_TEXT_ROWS = 6  # 헤더 + 앞 5행 (R-3.3 L1)
PDF_PAGES = 2
MAX_READ_BYTES = 50 << 20

TEXT_EXTS = {".txt", ".md", ".markdown", ".mdx", ".csv", ".tsv", ".json", ".log", ".xml", ".yaml", ".yml",
             ".ini", ".cfg", ".conf", ".sql", ".html", ".htm"}
_KIND = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx", ".pptx": "pptx"}


@dataclass
class Extracted:
    kind: str  # pdf|docx|xlsx|pptx|text|image|binary
    title: str = ""
    text: str = ""
    preview: list[str] = field(default_factory=list)


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _KIND:
        return _KIND[ext]
    if ext in TEXT_EXTS:
        return "text"
    if ext in IMAGE_EXTS:
        return "image"
    return "binary"


def _read_bytes(path: Path) -> bytes | None:
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_READ_BYTES:
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        try:
            chunks = []
            while chunk := os.read(fd, 1 << 20):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    except OSError:
        return None


def _clean(s: object) -> str:
    return " ".join(str(s).split())


# ---------- 형식별: (title, body_lines, preview_lines) ----------

def _docx(data: bytes) -> tuple[str, list[str], list[str]]:
    from docx import Document

    doc = Document(io.BytesIO(data))
    paras = [_clean(p.text) for p in doc.paragraphs]
    paras = [p for p in paras if p]
    for t in doc.tables[:5]:
        for row in t.rows[:20]:
            cells = [_clean(c.text) for c in row.cells]
            if any(cells):
                paras.append(" | ".join(cells))
    title = ""
    for p in doc.paragraphs:
        if p.text.strip() and (p.style is not None and (p.style.name or "").lower().startswith(("title", "heading"))):
            title = _clean(p.text)
            break
    title = title or (paras[0] if paras else "")
    return title, paras, paras[:PREVIEW_LINES]


def _xlsx(data: bytes) -> tuple[str, list[str], list[str]]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    body: list[str] = []
    preview: list[str] = []
    title = ""
    try:
        for i, ws in enumerate(wb.worksheets[:5]):
            body.append(f"[시트] {ws.title}")
            if i == 0:
                preview.append(f"[시트] {ws.title}")
            n = 0
            for row in ws.iter_rows(max_row=XLSX_PREVIEW_ROWS, values_only=True):
                vals = [_clean(v) for v in row if v is not None and str(v).strip()]
                if not vals:
                    continue
                line = " | ".join(vals)
                if not title:
                    title = vals[0]
                if n < XLSX_TEXT_ROWS:
                    body.append(line)
                if i == 0:
                    preview.append(line)
                n += 1
        if len(wb.worksheets) > 1:
            preview.append("시트: " + ", ".join(ws.title for ws in wb.worksheets[:20]))
    finally:
        wb.close()
    title = title or (wb.sheetnames[0] if wb.sheetnames else "")
    return title, body, preview[:PREVIEW_LINES]


def _pptx(data: bytes) -> tuple[str, list[str], list[str]]:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    titles: list[str] = []
    body: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        t = ""
        try:
            if slide.shapes.title is not None:
                t = _clean(slide.shapes.title.text)
        except Exception:  # noqa: BLE001
            t = ""
        titles.append(f"{i}. {t}" if t else f"{i}. (제목 없음)")
        if t:
            body.append(t)
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame is not None:
                txt = _clean(shape.text_frame.text)
                if txt and txt != t:
                    body.append(txt)
    first = next((x.split(". ", 1)[1] for x in titles if not x.endswith("(제목 없음)")), "")
    return first, body, titles[:PREVIEW_LINES]


def _pdf(data: bytes) -> tuple[str, list[str], list[str]]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    lines: list[str] = []
    for page in reader.pages[:PDF_PAGES]:
        txt = page.extract_text() or ""
        lines += [_clean(x) for x in txt.splitlines() if x.strip()]
    title = lines[0] if lines else ""
    if not title:
        meta = reader.metadata
        title = _clean(meta.title) if meta is not None and meta.title else ""
    return title, lines, lines[:PREVIEW_LINES]


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _text(data: bytes) -> tuple[str, list[str], list[str]]:
    txt = _decode_text(data[:256 * 1024])
    lines = [x.rstrip() for x in txt.splitlines()]
    nonempty = [x for x in lines if x.strip()]
    title = nonempty[0].lstrip("# ").strip() if nonempty else ""
    preview = [x[:PREVIEW_LINE_CHARS] for x in lines[:PREVIEW_LINES]]
    return _clean(title), lines, preview


_EXTRACTORS = {"docx": _docx, "xlsx": _xlsx, "pptx": _pptx, "pdf": _pdf, "text": _text}


def _is_log(path: Path) -> bool:
    low = path.name.lower()
    return path.suffix.lower() == ".log" or (path.suffix.lower() == ".txt" and "log" in low)


def extract(path: Path, level: str, max_chars: int = 1500, scope: "Scope | None" = None) -> Extracted:
    """등급별 텍스트·미리보기 추출. scope를 주면 READ 검사를 통과해야 읽는다(실패 시 빈 결과)."""
    path = Path(path)
    kind = kind_of(path)
    if level == "L3":
        return Extracted(kind, "", "", [L3_NOTICE])  # 열지도 않는다
    if kind in ("image", "binary"):
        return Extracted(kind)
    if scope is not None:
        from tongdory2.fsx.scope import Access

        if not scope.check(path, Access.READ).allowed:
            return Extracted(kind)
    data = _read_bytes(path)
    if data is None:
        return Extracted(kind)
    try:
        title, body, preview = _EXTRACTORS[kind](data)
    except Exception:  # noqa: BLE001 — 손상·암호화·형식 불일치 파일
        return Extracted(kind)

    preview = [ln[:PREVIEW_LINE_CHARS] for ln in preview][:PREVIEW_LINES]
    title = mask(title)[:TITLE_MAX]
    if level == "L2":
        if _is_log(path):
            title = ""  # 로그는 파일명만 (R-3.3)
        return Extracted(kind, title, title, preview)
    text = mask("\n".join(body))[:max_chars]
    return Extracted(kind, title, text, preview)
