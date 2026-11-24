"""D-04 현재 과제 트리 vs 명세 비교 (02_정책 §1.7).

| 상태 | 의미 |
| MISSING  | 명세 노드인데 폴더 없음 → 승인 시 빈 폴더 생성 제안 |
| MATCH    | 명세 노드이고 폴더 있음 → 목적지 후보(JOIN) |
| EXTRA    | 명세 외 기존 폴더 → 보존(이름 변경·삭제 금지) |
| MESSY    | 엉망 폴더 → 목적지 제외, 안의 파일은 재배치 대상 |

폴더 비교는 표기가 달라도(번호·구분자·동의어) 같은 노드로 본다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tongdory2.spec.extract_standard import SKIP_DIRS, SpecNode
from tongdory2.spec.normalize import is_messy, parse


@dataclass
class CompareRow:
    status: str  # MISSING | MATCH | EXTRA | MESSY
    node_id: str | None
    spec_path: str | None  # 명세 표기 경로
    actual_path: str | None  # 실제 상대 경로
    note: str = ""


def _children(p: Path, allow: Callable[[Path], bool]) -> list[Path]:
    if not allow(p):
        return []
    try:
        return sorted(
            Path(e.path) for e in os.scandir(p)
            if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")
        )
    except OSError:
        return []


def compare(
    project_root: Path,
    nodes: list[SpecNode],
    profile: str = "build",
    allow_read: Callable[[Path], bool] = lambda p: True,
) -> list[CompareRow]:
    active = [n for n in nodes if n.status == "ADOPTED" and n.profile in ("common", profile)]
    kids: dict[str | None, list[SpecNode]] = {}
    for n in active:
        kids.setdefault(n.parent_id, []).append(n)

    rows: list[CompareRow] = []

    def walk(spec_parent: str | None, actual_dir: Path | None, rel: str) -> None:
        spec_kids = kids.get(spec_parent, [])
        actual = _children(actual_dir, allow_read) if actual_dir else []
        matched: set[Path] = set()
        for sn in sorted(spec_kids, key=lambda x: x.display_path):
            hit = next(
                (a for a in actual if a not in matched and parse(a.name).key == sn.key
                 and (parse(a.name).number in (None, parse(sn.segment).number) or sn.parent_id is None)),
                None,
            ) or next((a for a in actual if a not in matched and parse(a.name).key == sn.key), None)
            if hit is not None:
                matched.add(hit)
                arel = f"{rel}/{hit.name}" if rel else hit.name
                note = "" if hit.name == sn.segment else f"표기 차이: 실제 '{hit.name}'"
                rows.append(CompareRow("MATCH", sn.node_id, sn.display_path, arel, note))
                if sn.role != "PROTECTED":
                    walk(sn.node_id, hit, arel)
            else:
                rows.append(CompareRow("MISSING", sn.node_id, sn.display_path, None, "승인 시 빈 폴더 생성"))
                walk(sn.node_id, None, f"{rel}/{sn.segment}" if rel else sn.segment)
        for a in actual:
            if a in matched or a.name in SKIP_DIRS:
                continue
            arel = f"{rel}/{a.name}" if rel else a.name
            if is_messy(a.name):
                rows.append(CompareRow("MESSY", None, None, arel, "정리 필요 — 안의 파일은 재배치 대상"))
            else:
                rows.append(CompareRow("EXTRA", None, None, arel, "명세 외 폴더 — 보존"))

    walk(None, project_root, "")
    return rows
