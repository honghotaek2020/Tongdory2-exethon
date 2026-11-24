"""단일 안전 게이트 — 파일을 바꾸는 모든 작업은 실행 직전에 이 검사를 통과해야 한다.

03_도구_데이터명세 §3.3 (결정 #37 반영). 지원 작업: MKDIR · MOVE · TRASH · RMDIR_EMPTY(Undo 전용).
영구 삭제(DELETE)는 존재하지 않는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from tongdory2.fsx.paths import is_within
from tongdory2.fsx.roots import Role
from tongdory2.fsx.scope import Access, Scope
from tongdory2.fsx.sensitivity import _L2_NAME  # 계약서·견적서류 이름 신호
from tongdory2.spec.normalize import is_messy

PROTECTED_PARTS = {".git", "node_modules", "build", "dist", "target", ".venv", "venv", "__pycache__"}


class Op(str, Enum):
    MKDIR = "MKDIR"
    MOVE = "MOVE"
    TRASH = "TRASH"
    RMDIR_EMPTY = "RMDIR_EMPTY"


class Decision(str, Enum):
    ALLOW = "ALLOW"
    SKIP = "SKIP"
    DENY = "DENY"


@dataclass
class PlanOp:
    seq: int
    op: Op
    src: str | None
    dst: str
    entry_id: str | None = None
    fingerprint: dict | None = None  # {size, mtime_ns} at plan time
    sensitivity: str = "L1"
    kind: str = "DEST"  # DEST | HOLD | TRASH | MKDIR
    note: str = ""


@dataclass
class GateContext:
    scope: Scope
    current_root: Path
    storage_root: Path
    allowed_dest_dirs: set[str]  # 명세 노드·승인된 새 노드의 실제 경로 + 보류함 날짜 폴더
    planned_mkdirs: set[str] = field(default_factory=set)
    acks: set[str] = field(default_factory=set)
    recent_hours: float = 2.0
    is_git_repo: bool = False
    now: float = 0.0
    settings_key: str | None = None


@dataclass
class GateResult:
    decision: Decision
    reason: str
    detail: str = ""


def _fp(p: Path) -> dict:
    st = os.lstat(p)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def gate(op: PlanOp, ctx: GateContext) -> GateResult:
    dst = Path(op.dst)
    src = Path(op.src) if op.src else None

    # 0. 등록 범위·역할 (scope가 링크 구성요소·거부 경로까지 검사)
    if op.op == Op.MKDIR:
        r = ctx.scope.check(dst, Access.MKDIR)
        if not r.allowed:
            return GateResult(Decision.DENY, r.reason.value, str(dst))
    elif op.op in (Op.MOVE, Op.TRASH):
        rs = ctx.scope.check(src, Access.MOVE_SRC if op.op == Op.MOVE else Access.TRASH_SRC)
        if not rs.allowed:
            return GateResult(Decision.DENY, rs.reason.value, "원본")
        rd = ctx.scope.check(dst, Access.MOVE_DST if op.op == Op.MOVE else Access.TRASH_DST)
        if not rd.allowed:
            return GateResult(Decision.DENY, rd.reason.value, "목적지")
        if op.op == Op.MOVE and rd.root and rd.root.role not in (Role.CURRENT, Role.STORAGE):
            return GateResult(Decision.DENY, "ROLE_DENIED", "목적지는 현재 과제 또는 저장 위치만")
    elif op.op == Op.RMDIR_EMPTY:
        r = ctx.scope.check(dst, Access.MKDIR)
        if not r.allowed:
            return GateResult(Decision.DENY, r.reason.value, str(dst))
    else:  # 2. 작업 화이트리스트
        return GateResult(Decision.DENY, "OP_NOT_ALLOWED", str(op.op))

    # 6. 목적지 보호: 소스 영역·빌드·.git·엉망 폴더
    try:
        rel = dst.relative_to(ctx.current_root)
        parts = rel.parts
        if parts and (parts[0].endswith("source") or parts[0].endswith("소스") or parts[0] == "src"):
            return GateResult(Decision.DENY, "DEST_PROTECTED", "소스 영역")
        if any(p in PROTECTED_PARTS for p in parts) or any(is_messy(p) for p in parts[:-1] if op.op == Op.MOVE):
            return GateResult(Decision.DENY, "DEST_PROTECTED", "보호·정리 필요 폴더")
    except ValueError:
        pass

    if op.op == Op.MKDIR:
        if str(dst) not in ctx.planned_mkdirs:
            return GateResult(Decision.DENY, "MKDIR_NOT_IN_SPEC", str(dst))
        return GateResult(Decision.ALLOW, "OK")

    if op.op == Op.RMDIR_EMPTY:
        if not dst.is_dir() or any(dst.iterdir()):
            return GateResult(Decision.SKIP, "NOT_EMPTY")
        return GateResult(Decision.ALLOW, "OK")

    # 4. 원본 상태: 정규 파일, 계획 시점과 동일
    if src is None or not src.exists() or src.is_symlink():
        return GateResult(Decision.SKIP, "SRC_MISSING")
    if not src.is_file():
        return GateResult(Decision.DENY, "NOT_A_FILE")
    if op.fingerprint and _fp(src) != op.fingerprint:
        return GateResult(Decision.SKIP, "SRC_CHANGED", "계획 이후 파일이 바뀜")
    # 5. 최근 수정
    if ctx.now and (ctx.now - src.stat().st_mtime) < ctx.recent_hours * 3600:
        return GateResult(Decision.SKIP, "RECENTLY_MODIFIED")

    # 7. 목적지 허용 목록 (명세 노드·새 노드·보류함·휴지통)
    if op.op == Op.MOVE and str(dst.parent) not in ctx.allowed_dest_dirs:
        return GateResult(Decision.DENY, "DEST_NOT_IN_SPEC", str(dst.parent))

    # 9. 덮어쓰기 금지 (실행 시 link로 다시 막음)
    if dst.exists() or dst.is_symlink():
        return GateResult(Decision.SKIP, "NAME_CONFLICT")

    # 10. 같은 볼륨 (복사 후 삭제 금지)
    try:
        dparent = dst.parent if dst.parent.exists() else _first_existing(dst.parent)
        if os.stat(src).st_dev != os.stat(dparent).st_dev:
            return GateResult(Decision.DENY, "CROSS_VOLUME")
    except OSError:
        return GateResult(Decision.DENY, "CROSS_VOLUME")

    # 11·12. 민감·Git 저장소
    in_repo = ctx.is_git_repo and is_within(dst, ctx.current_root)
    if op.sensitivity == "L3" and op.op == Op.MOVE:
        return GateResult(Decision.DENY, "SENSITIVE")
    if in_repo and op.op == Op.MOVE and "GIT_REPO_DEST" not in ctx.acks:
        return GateResult(Decision.DENY, "ACK_REQUIRED", "Git 저장소로 이동 확인 필요")
    if in_repo and op.sensitivity == "L2" and _L2_NAME.search(src.name.lower()) and "L2_TO_REPO" not in ctx.acks:
        return GateResult(Decision.DENY, "ACK_REQUIRED", "계약서·견적서 등을 저장소에 넣는 것 확인 필요")
    return GateResult(Decision.ALLOW, "OK")


def _first_existing(p: Path) -> Path:
    while not p.exists():
        p = p.parent
    return p
