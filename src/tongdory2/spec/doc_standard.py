"""D-02 문서화된 표준 탐지·해석 (02_정책 §1.6, R-1.18).

후보: 과제 루트와 착수 단계 폴더 안의 xlsx 중 파일명이 `산출물 목록|폴더 구조|folder guide|가이드`이고
헤더에 `단계`와 `산출물`이 모두 있는 시트. 표 행 → (단계, 산출물). 행 해석은 규칙 기반(LLM 없음).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tongdory2.spec.normalize import TOP_IDS, compare_key, parse

_NAME_RE = re.compile(r"(산출물\s*목록|폴더\s*구조|folder.?guide|가이드)", re.I)
_NOISE_SUFFIX = re.compile(r"\s*(요약|샘플|사본|양식|템플릿|template)$", re.I)
_PAREN = re.compile(r"\(.*?\)")
ENTRY_PHASES = {"진입점", "entry", "루트"}


@dataclass(frozen=True)
class StdRow:
    phase: str
    deliverable: str
    required: bool
    source: str  # 과제/상대경로

    @property
    def is_entry(self) -> bool:
        return compare_key(self.phase) in ENTRY_PHASES

    @property
    def phase_number(self) -> int | None:
        return parse(self.phase).number

    @property
    def phase_name(self) -> str:
        return parse(self.phase).name

    @property
    def phase_key(self) -> str:
        return parse(self.phase).key

    @property
    def deliverable_name(self) -> str:
        n = _PAREN.sub("", self.deliverable).strip()
        return _NOISE_SUFFIX.sub("", n).strip() or n

    @property
    def deliverable_key(self) -> str:
        return compare_key(self.deliverable_name)


@dataclass
class StdDoc:
    project: str
    rel_path: str
    rows: list[StdRow]


def _candidate_files(root: Path, allow: Callable[[Path], bool]) -> list[Path]:
    dirs = [root]
    try:
        for top in os.scandir(root):
            if top.is_dir(follow_symlinks=False) and TOP_IDS.get(parse(top.name).key) == "doc":
                for c in os.scandir(top.path):
                    if c.is_dir(follow_symlinks=False) and parse(c.name).key == "착수":
                        dirs.append(Path(c.path))
    except OSError:
        pass
    out: list[Path] = []
    for d in dirs:
        if not allow(d):
            continue
        try:
            for e in sorted(os.scandir(d), key=lambda e: e.name):
                if (e.is_file(follow_symlinks=False) and e.name.lower().endswith(".xlsx")
                        and _NAME_RE.search(e.name) and allow(Path(e.path))):
                    out.append(Path(e.path))
        except OSError:
            continue
    return out


def read_phase_rows(header: tuple, rows) -> list[tuple[str, str, bool]] | None:
    """단계·산출물 헤더가 맞으면 해당 표의 내용을 읽는다."""
    labels = [str(cell or "").strip() for cell in header]
    phase = next((i for i, label in enumerate(labels) if "단계" in label), None)
    output = next((i for i, label in enumerate(labels) if "산출물" in label), None)
    required = next((i for i, label in enumerate(labels) if "필수" in label), None)
    if phase is None or output is None:
        return None

    def value(row, index: int) -> str:
        return str(row[index] or "").strip() if index < len(row) else ""

    result = []
    for row in rows:
        phase_name, output_name = value(row, phase), value(row, output)
        if phase_name and output_name:
            result.append((phase_name, output_name, required is not None and value(row, required) == "필수"))
    return result


def _read_xlsx(path: Path) -> list[tuple[str, str, bool]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            for header in rows:
                parsed = read_phase_rows(header, rows)
                if parsed is not None:
                    return parsed
        return []
    finally:
        wb.close()


def find_standards(project_roots: list[Path], allow: Callable[[Path], bool] = lambda p: True) -> list[StdDoc]:
    docs: list[StdDoc] = []
    for root in project_roots:
        for f in _candidate_files(root, allow):
            try:
                rows = _read_xlsx(f)
            except Exception:  # 손상 파일 등은 표준 후보에서 제외
                continue
            if rows:
                rel = str(f.relative_to(root))
                docs.append(StdDoc(root.name, rel, [StdRow(p, d, r, f"{root.name}/{rel}") for p, d, r in rows]))
    return docs
