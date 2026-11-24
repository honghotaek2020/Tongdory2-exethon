"""⑤ 저장 위치 내부 구조 생성 (결정 #37·#38).

<저장 위치>/01.보류함, 02.지식센터, 03.내보내기 — 없으면 만들고, 있으면 그대로 둔다.
쓰기 전에 반드시 Scope 검사를 통과해야 한다.
"""

from __future__ import annotations

from pathlib import Path

from tongdory2.fsx.roots import STORAGE_EXPORT, STORAGE_HOLD, STORAGE_KC, RegisteredRoot, Role
from tongdory2.fsx.scope import Access, Scope

LAYOUT = (STORAGE_HOLD, STORAGE_KC, STORAGE_EXPORT)


def ensure_layout(root: RegisteredRoot, scope: Scope) -> list[Path]:
    if root.role != Role.STORAGE:
        raise ValueError("저장 위치(⑤) 역할이 아닙니다")
    created: list[Path] = []
    targets = [root.path] + [root.path / name for name in LAYOUT]
    for t in targets:
        if t.exists():
            continue
        res = scope.check(t, Access.MKDIR)
        if not res.allowed:
            raise PermissionError(f"범위 검사 실패: {res.reason.value}")
        t.mkdir(exist_ok=False)
        created.append(t)
    return created
