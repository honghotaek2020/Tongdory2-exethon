"""카탈로그 선택 → 실행 계획(Plan). 계획은 서버가 ID로 보관하고, 실행 직전 다시 게이트를 통과한다.

목적지 결정 (사람 지정이 우선):
- override = 노드 ID → 그 노드 경로 / HOLD → ⑤ 01.보류함/<날짜>/ / TRASH → OS 휴지통(중복·사람 지정만)
- verdict JOIN·SPEC_NODE·NEW_NODE → node_path / DUP → 휴지통
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from tongdory2.fsx.hashing import version_key
from tongdory2.fsx.roots import STORAGE_HOLD, RegisteredRoot
from tongdory2.fsx.scan import ScanItem, scan_collect
from tongdory2.fsx.scope import Scope
from tongdory2.fsx.sensitivity import _L2_NAME as _CONTRACT
from tongdory2.fsx.sensitivity import classify_sensitivity
from tongdory2.plan.safety import Op, PlanOp

AUTO_DESTINATION_VERDICTS = frozenset(("JOIN", "SPEC_NODE", "NEW_NODE", "GROUP"))
DESTINATION_VERDICTS = AUTO_DESTINATION_VERDICTS | {"ETC"}
MOVABLE = DESTINATION_VERDICTS | {"DUP"}
VERSION_LOOKUP_LIMIT = 2_000  # 계획 미리보기에서 확인할 파일·폴더 이름 수


def destination_folder(entry: dict, root: Path) -> Path | None:
    path = entry.get("node_path")
    return root / path if path and entry["verdict"] in DESTINATION_VERDICTS else None


def missing_destination_folders(entries: list[dict], selected: set[str], root: Path) -> dict[Path, list[str]]:
    """자동 분류로 선택된 파일 중 제안된 목적지 폴더를 만들어야 하는 항목."""
    by_folder: dict[Path, list[str]] = {}
    for entry in entries:
        if (entry["entry_id"] not in selected or entry.get("override") or entry.get("sensitivity") == "L3"
                or entry["verdict"] not in AUTO_DESTINATION_VERDICTS):
            continue
        folder = destination_folder(entry, root)
        if folder is not None and folder.is_relative_to(root) and ".." not in folder.parts:
            by_folder.setdefault(folder, []).append(entry["entry_id"])
    return {folder: ids for folder, ids in by_folder.items() if not folder.exists()}


@dataclass
class Plan:
    ops: list[PlanOp]
    mkdirs: list[str]
    allowed_dest_dirs: list[str]
    warnings: list[str] = field(default_factory=list)
    acks_needed: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    is_git_repo: bool = False

    def to_dict(self) -> dict:
        return {
            "is_git_repo": self.is_git_repo,
            "ops": [asdict(o) | {"op": o.op.value} for o in self.ops],
            "mkdirs": self.mkdirs, "allowed_dest_dirs": self.allowed_dest_dirs,
            "warnings": self.warnings, "acks_needed": self.acks_needed, "skipped": self.skipped,
        }


def related_saved_versions(plan: Plan, root: RegisteredRoot, scope: Scope) -> int:
    """저장 위치의 임시 폴더를 읽어 관련 판본의 수만 확인한다."""
    additions = [op for op in plan.ops if op.op == Op.MOVE and op.kind == "DEST" and op.src]
    keys = {key for op in additions if (key := version_key(Path(op.src).name, explicit=True))}
    if not keys:
        return 0
    planned = {Path(path) for op in additions for path in (op.src, op.dst)}
    related: set[Path] = set()

    def consider(item: ScanItem) -> None:
        if (not item.is_dir and not item.is_link and item.path not in planned
                and version_key(item.name, explicit=True) in keys and classify_sensitivity(item.path).level != "L3"):
            related.add(item.path)

    scan_collect([root], scope, on_item=consider, visit_limit=VERSION_LOOKUP_LIMIT)
    return len(related)


def _unique(dst: Path, taken: set[str]) -> Path:
    if not dst.exists() and str(dst) not in taken:
        return dst
    stem, suf = dst.stem, dst.suffix
    i = 1
    while True:
        cand = dst.with_name(f"{stem}_{i}{suf}")
        if not cand.exists() and str(cand) not in taken:
            return cand
        i += 1


def build_plan(
    entries: list[dict],
    selected: set[str],
    spec_nodes: list[dict],
    current_root: Path,
    storage_root: Path,
    home: Path,
    is_git_repo: bool,
    today: date | None = None,
) -> Plan:
    today = today or date.today()
    node_path = {n["node_id"]: n["display_path"] for n in spec_nodes if n["status"] == "ADOPTED"}
    ops: list[PlanOp] = []
    mk: list[str] = []
    allowed: set[str] = set()
    taken: set[str] = set()
    skipped: list[dict] = []
    warnings: list[str] = []
    acks: set[str] = set()
    hold_dir = storage_root / STORAGE_HOLD / today.isoformat()

    def ensure_dirs(d: Path, base: Path) -> None:
        chain = []
        cur = d
        while cur != base and not cur.exists():
            chain.append(cur)
            cur = cur.parent
        for c in reversed(chain):
            if str(c) not in mk:
                mk.append(str(c))

    for e in entries:
        ov = e.get("override")
        if e["entry_id"] not in selected and not ov:
            continue
        src = Path(e["path"])
        kind = "DEST"
        if ov == "HOLD":
            dst_dir, kind = hold_dir, "HOLD"
        elif ov == "TRASH" or (not ov and e["verdict"] == "DUP"):
            dst_dir, kind = home / ".Trash", "TRASH"
        elif ov and ov in node_path:
            dst_dir = current_root / node_path[ov]
        elif ov == "CLEAR":
            continue
        else:
            dst_dir = destination_folder(e, current_root)
            if dst_dir is None:
                skipped.append({"entry_id": e["entry_id"], "name": e["name"], "reason": "옮길 폴더가 정해지지 않아 이동하지 않음"})
                continue
        if e.get("sensitivity") == "L3":
            skipped.append({"entry_id": e["entry_id"], "name": e["name"], "reason": "민감 파일 — 이동하지 않음"})
            continue
        if kind == "TRASH":
            dst = _unique(dst_dir / src.name, taken)  # 휴지통 안 이름만 OS 방식으로 구분
        else:
            dst = dst_dir / src.name
            if not dst.exists() and str(dst) in taken and kind == "DEST":
                # #72 같은 계획 안에서 같은 이름이 부딪치면 원래 상위 폴더 이름으로 하위 폴더를 만든다(이름은 그대로)
                from tongdory2.catalog.grouping import _clean

                for parent in list(src.parents)[:3]:
                    alt_dir = dst_dir / _clean(parent.name)
                    alt = alt_dir / src.name
                    if not alt.exists() and str(alt) not in taken:
                        dst_dir, dst = alt_dir, alt
                        break
            if dst.exists() or str(dst) in taken:  # 파일 이름은 바꾸지 않는다 — 건너뛰고 기록
                skipped.append({"entry_id": e["entry_id"], "name": e["name"],
                                "reason": "옮길 위치에 같은 이름의 파일이 있음 — 이름을 바꾸지 않고 건너뜀"})
                continue
        taken.add(str(dst))
        try:
            st = os.lstat(src)
            fp = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
        except OSError:
            skipped.append({"entry_id": e["entry_id"], "name": e["name"], "reason": "원본 없음"})
            continue
        if kind != "TRASH":
            ensure_dirs(dst_dir, current_root if kind == "DEST" else storage_root)
            allowed.add(str(dst_dir))
        note = "" if dst.name == src.name else f"같은 이름이 있어 '{dst.name}'으로"
        if kind == "DEST" and e.get("node_path") and not ov and dst.parent != current_root / e["node_path"]:
            note = f"같은 이름 파일이 있어 '{dst.parent.name}' 하위 폴더로"
        ops.append(PlanOp(0, Op.TRASH if kind == "TRASH" else Op.MOVE, str(src), str(dst), e["entry_id"], fp,
                          e.get("sensitivity", "L1"), kind, note))
        if kind == "DEST" and is_git_repo:
            acks.add("GIT_REPO_DEST")
            if e.get("sensitivity") == "L2" and _CONTRACT.search(e["name"].lower()):  # 계약서류만 (이미지·로그 제외)
                acks.add("L2_TO_REPO")

    mkops = [PlanOp(0, Op.MKDIR, None, d, kind="MKDIR") for d in mk]
    all_ops = mkops + ops
    for i, o in enumerate(all_ops, 1):
        o.seq = i
    if "GIT_REPO_DEST" in acks:
        warnings.append("현재 과제 폴더는 Git 저장소입니다. 옮긴 문서가 커밋될 수 있습니다.")
    if "L2_TO_REPO" in acks:
        warnings.append("계약서·견적서 같은 문서가 Git 저장소 안으로 들어갑니다.")
    return Plan(all_ops, mk, sorted(allowed | set(mk)), warnings, sorted(acks), skipped, is_git_repo)
