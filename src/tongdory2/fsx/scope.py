"""T-02 등록 범위 검사 `Scope.check()` — 모든 파일 읽기·쓰기 경로의 첫 관문.

규칙 (02_정책 §16.1 + 결정 #37):
- 경로의 realpath가 **확정된** 등록 루트 하위여야 한다. 가장 깊은 루트가 역할을 정한다.
- 거부 경로(~/Library 등)는 루트 안이어도 항상 범위 밖.
- 쓰기는 ② CURRENT · ⑤ STORAGE 두 곳만. 예외: DUP 휴지통 이동 목적지(OS 휴지통).
- 경로 구성요소에 심볼릭 링크가 있으면 거부(링크를 따라가지 않는다).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from tongdory2.fsx.paths import absnorm, first_symlink_component, is_within, real
from tongdory2.fsx.roots import RegisteredRoot, Role, is_denied_path


class Access(str, Enum):
    READ = "READ"  # 스캔·추출·해시·git 조회
    MOVE_SRC = "MOVE_SRC"
    MOVE_DST = "MOVE_DST"
    MKDIR = "MKDIR"
    COPY_SRC = "COPY_SRC"
    COPY_DST = "COPY_DST"
    WRITE_NEW = "WRITE_NEW"
    TRASH_SRC = "TRASH_SRC"
    TRASH_DST = "TRASH_DST"
    RESTORE_DST = "RESTORE_DST"  # Undo 전용: 기록된 원위치로 되돌리기


# 역할별 허용 접근. 쓰기 계열(MOVE_DST·MKDIR·COPY_DST·WRITE_NEW)은 ②·⑤에만.
PERMISSIONS: dict[Role, set[Access]] = {
    Role.BASELINE: {Access.READ},
    Role.CURRENT: {
        Access.READ, Access.MOVE_SRC, Access.MOVE_DST, Access.MKDIR, Access.COPY_SRC,
        Access.WRITE_NEW, Access.TRASH_SRC, Access.RESTORE_DST,
    },
    Role.COLLECT: {Access.READ, Access.MOVE_SRC, Access.COPY_SRC, Access.TRASH_SRC, Access.RESTORE_DST},
    Role.IN_PLACE: {Access.READ, Access.COPY_SRC},
    Role.STORAGE: {
        Access.READ, Access.MOVE_SRC, Access.MOVE_DST, Access.MKDIR, Access.COPY_DST,
        Access.WRITE_NEW, Access.RESTORE_DST,
    },
}


class ScopeReason(str, Enum):
    OK = "OK"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    DENIED_PATH = "DENIED_PATH"
    ROLE_DENIED = "ROLE_DENIED"
    SYMLINK_IN_PATH = "SYMLINK_IN_PATH"
    NOT_CONFIRMED = "NOT_CONFIRMED"


@dataclass(frozen=True)
class ScopeResult:
    allowed: bool
    reason: ScopeReason
    root: RegisteredRoot | None = None
    path: Path | None = None


class Scope:
    """확정된 등록 루트 집합에 대한 범위 판정기."""

    def __init__(self, roots: list[RegisteredRoot], home: Path, app_data: Path) -> None:
        self.home = real(home)
        self.app_data = app_data
        # 미확정 루트는 범위에 넣지 않는다 (R-16.6). 깊은 루트가 먼저 오도록 정렬.
        self.roots = sorted((r for r in roots if r.confirmed), key=lambda r: len(r.path.parts), reverse=True)
        self._has_unconfirmed = any(not r.confirmed for r in roots)

    @property
    def ready(self) -> bool:
        return bool(self.roots)

    def owner(self, p: Path) -> RegisteredRoot | None:
        for r in self.roots:
            if is_within(p, r.path):
                return r
        return None

    def check(self, raw: str | Path, access: Access) -> ScopeResult:
        p_abs = absnorm(raw)

        if access == Access.TRASH_DST:
            return self._check_trash(p_abs)

        if not self.roots:
            return ScopeResult(False, ScopeReason.NOT_CONFIRMED, path=p_abs)

        # 링크를 따라가지 않는다: 링크 해소 전 경로 기준으로 소유 루트를 찾고 구성요소를 검사
        root = self.owner(p_abs)
        if root is None:
            return ScopeResult(False, ScopeReason.OUT_OF_SCOPE, path=p_abs)
        link = first_symlink_component(p_abs, root.path)
        if link is not None:
            return ScopeResult(False, ScopeReason.SYMLINK_IN_PATH, root, p_abs)

        p = real(p_abs) if p_abs.exists() else p_abs
        if not is_within(p, root.path):  # 방어: 해소 후 이탈
            return ScopeResult(False, ScopeReason.OUT_OF_SCOPE, path=p)
        if is_denied_path(p, self.home, self.app_data):
            return ScopeResult(False, ScopeReason.DENIED_PATH, root, p)
        if access not in PERMISSIONS[root.role]:
            return ScopeResult(False, ScopeReason.ROLE_DENIED, root, p)
        return ScopeResult(True, ScopeReason.OK, root, p)

    def link_target_in_scope(self, link_path: Path) -> bool:
        """R-16.2: 링크 대상 realpath가 등록 범위 안인가 (EX_LINK vs OUT_OF_SCOPE 구분용)."""
        target = real(link_path)
        return self.owner(target) is not None and not is_denied_path(target, self.home, self.app_data)

    def _check_trash(self, p: Path) -> ScopeResult:
        trash = self.home / ".Trash"
        if p.parent == trash:
            if first_symlink_component(p, self.home) is not None:
                return ScopeResult(False, ScopeReason.SYMLINK_IN_PATH, None, p)
            return ScopeResult(True, ScopeReason.OK, None, p)
        return ScopeResult(False, ScopeReason.ROLE_DENIED, None, p)
