"""D-01 과거 과제 트리 → 공통 구조 추출 (02_정책 §1.1~§1.5).

- 과거 과제는 읽기 전용: 디렉터리 목록과 파일명만 본다 (R-1.1, R-1.3)
- `01 source`·`.git`·빌드 폴더 안으로 들어가지 않는다 (R-1.2)
- 같은 프로필 과제 N개 중 ⌈2N/3⌉개 이상 → 채택, 미만 → 후보 (R-1.8)
- 누락 단계는 빼지 않고 "없음"으로 표시 (R-1.10)
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from tongdory2.spec.defaults import MAINT_KEYWORDS, describe
from tongdory2.spec.doc_standard import StdRow
from tongdory2.spec.normalize import TOP_IDS, Segment, fmt_child, fmt_top, is_messy, parse

SKIP_DIRS = {".git", "node_modules", "build", "dist", "target", ".venv", "venv", "__pycache__", ".idea", ".vscode"}
MAX_EXEMPLARS = 10


@dataclass
class SpecNode:
    node_id: str
    parent_id: str | None
    profile: str  # common | build | maint
    segment: str
    display_path: str
    label: str
    key: str
    description: str
    role: str  # DEST | PROTECTED | CONTAINER
    status: str  # ADOPTED | CANDIDATE
    support_count: int
    support_total: int
    support_projects: list[str]
    missing_in: list[str]
    aliases: list[str] = field(default_factory=list)
    exemplar_filenames: list[str] = field(default_factory=list)
    origin: str = "EXTRACTED"
    order: int = 0
    hint: str = ""
    required_for_handover: bool = False

    @property
    def embedding_text(self) -> str:
        parts = [self.label, self.description]
        if self.aliases:
            parts.append(", ".join(self.aliases))
        if self.exemplar_filenames:
            parts.append(", ".join(self.exemplar_filenames))
        return " | ".join(parts)


@dataclass
class Extraction:
    projects: list[str]
    profiles: dict[str, str]  # project → build | maint
    nodes: list[SpecNode]
    variants: list[dict]  # [{node_id, display_path, observed: {project: original|None}}]
    messy: dict[str, list[str]]  # project → 엉망 폴더 상대경로
    entries: list[dict] = field(default_factory=list)  # 진입점 파일(README 등) — 문서 표준 기재
    std_unmatched: list[dict] = field(default_factory=list)  # 문서엔 있는데 실제 폴더 없음

    def to_dict(self) -> dict:
        d = asdict(self)
        for n, src in zip(d["nodes"], self.nodes):
            n["embedding_text"] = src.embedding_text
        return d


@dataclass
class _Obs:
    project: str
    seg: Segment
    path: Path


def _list_dirs(p: Path, allow: Callable[[Path], bool]) -> list[Path]:
    if not allow(p):
        return []
    try:
        entries = sorted(os.scandir(p), key=lambda e: e.name)
    except OSError:
        return []
    return [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]


def _list_files(p: Path, allow: Callable[[Path], bool]) -> list[str]:
    if not allow(p):
        return []
    try:
        return sorted(
            e.name for e in os.scandir(p) if e.is_file(follow_symlinks=False) and not e.name.startswith(".")
        )
    except OSError:
        return []


def _profile_of(doc_children: list[Path]) -> str:
    keys = {parse(c.name).key for c in doc_children}
    return "maint" if len(keys & MAINT_KEYWORDS) >= 2 else "build"


def extract(
    project_roots: list[Path],
    allow_read: Callable[[Path], bool] = lambda p: True,
    standard: "list[StdRow] | None" = None,
    workspace: bool = False,
) -> Extraction:
    """workspace=True: 과거 과제 없이 **현재 폴더 하나의 기존 하위 폴더**를 그대로 명세로 쓴다(업무 작업공간).
    폴더 표기는 실제 이름 그대로 유지하고, 사람·출처·연도 폴더는 설명에 그 축을 적는다."""
    """project_roots: 최신 과제가 앞에 오도록 정렬해서 넘긴다 (동점 시 최신 우선)."""
    projects = [p.name for p in project_roots]
    profiles: dict[str, str] = {}
    messy: dict[str, list[str]] = defaultdict(list)
    # 관찰: 키 경로 → [_Obs]. 키 경로 = (profile, top_id, child_key?, grandchild_key?)
    obs: dict[tuple, list[_Obs]] = defaultdict(list)

    for root in project_roots:
        pname = root.name
        tops = _list_dirs(root, allow_read)
        doc_dirs = [t for t in tops if TOP_IDS.get(parse(t.name).key) == "doc"]
        doc_children = [c for d in doc_dirs for c in _list_dirs(d, allow_read)]
        profile = _profile_of(doc_children)
        profiles[pname] = profile

        for t in tops:
            seg = parse(t.name)
            if is_messy(t.name) or t.name in SKIP_DIRS:
                messy[pname].append(t.name)
                continue
            top_id = TOP_IDS.get(seg.key) or "x" + hashlib.sha1(seg.key.encode()).hexdigest()[:6]
            obs[("common", top_id)].append(_Obs(pname, seg, t))
            if top_id not in ("doc", "etc"):
                continue  # src 보호(R-1.2), env·허브 폴더는 하위를 노드로 만들지 않음
            child_profile = profile if top_id == "doc" else "common"
            for c in _list_dirs(t, allow_read):
                cseg = parse(c.name)
                if is_messy(c.name):
                    messy[pname].append(f"{t.name}/{c.name}")
                    continue
                obs[(child_profile, top_id, cseg.key)].append(_Obs(pname, cseg, c))
                for g in _list_dirs(c, allow_read):
                    gseg = parse(g.name)
                    if is_messy(g.name):
                        messy[pname].append(f"{t.name}/{c.name}/{g.name}")
                        continue
                    if gseg.name.isdigit():  # 연도 폴더(정기점검/2024) 등은 노드가 아니라 내용
                        continue
                    obs[(child_profile, top_id, cseg.key, gseg.key)].append(_Obs(pname, gseg, g))

    totals = {
        "common": len(project_roots),
        "build": sum(1 for v in profiles.values() if v == "build"),
        "maint": sum(1 for v in profiles.values() if v == "maint"),
    }

    def majority_number(items: list[_Obs]) -> int | None:
        nums = [o.seg.number for o in items if o.seg.number is not None]
        if not nums:
            return None
        cnt = Counter(nums)
        best = max(cnt.values())
        for o in items:  # 동점이면 최신 과제(앞쪽) 우선
            if o.seg.number is not None and cnt[o.seg.number] == best:
                return o.seg.number
        return None

    def majority_name(items: list[_Obs]) -> str:
        cnt = Counter(o.seg.name for o in items)
        best = max(cnt.values())
        return next(o.seg.name for o in items if cnt[o.seg.name] == best)

    # ---- A. 키 경로별 원자료 ----
    raw: dict[tuple, dict] = {}
    for kp, items in obs.items():
        raw[kp] = {"num": majority_number(items), "name": majority_name(items), "items": items,
                   "key": items[0].seg.key, "std": None}

    # ---- B. 문서 표준 반영 (R-1.9: 표준에 있는 노드는 집계와 무관하게 채택) ----
    entries: list[dict] = []
    std_unmatched: list[dict] = []
    if standard:
        for i, row in enumerate(standard):
            if row.is_entry:
                entries.append({"name": row.deliverable, "required": row.required, "order": i})
                continue
            phase_kp = _match_child(raw, ("common", "doc"), row.phase_key, prefer_profile="build")
            if phase_kp is None:
                phase_kp = ("build", "doc", row.phase_key)
                raw[phase_kp] = {"num": row.phase_number, "name": row.phase_name, "items": [], "key": row.phase_key, "std": None}
            raw[phase_kp]["std"] = raw[phase_kp]["std"] if raw[phase_kp]["std"] is not None else i
            raw[phase_kp].setdefault("std_required", False)
            kids = [k for k in raw if len(k) == 4 and k[:3] == phase_kp]
            target = _match_child(raw, phase_kp, row.deliverable_key)
            if target is not None:
                raw[target]["std"] = i if raw[target]["std"] is None else raw[target]["std"]
                raw[target]["std_required"] = raw[target].get("std_required") or row.required
            elif kids:  # 하위 노드가 있는 단계인데 맞는 하위가 없음 → 표준 기재 새 노드
                nk = phase_kp + (row.deliverable_key,)
                raw[nk] = {"num": None, "name": row.deliverable_name, "items": [], "key": row.deliverable_key,
                           "std": i, "std_required": row.required}
                std_unmatched.append({"phase": row.phase, "deliverable": row.deliverable, "note": "문서엔 있는데 실제 폴더 없음"})
            else:  # 하위가 없는 단계(착수·사용자매뉴얼 등) → 단계 자체가 산출물 위치
                raw[phase_kp]["std_required"] = raw[phase_kp].get("std_required") or row.required

    # ---- C. 번호 확정: 번호 없는 형제는 표준 행 순서 → 이름 순 (R-1.6) ----
    groups: dict[tuple | None, list[tuple]] = defaultdict(list)
    for kp in raw:
        groups[_parent_kp(kp)].append(kp)
    for parent, kids in groups.items():
        if parent is None:
            continue
        used = {raw[k]["num"] for k in kids if raw[k]["num"] is not None}
        nxt = 1
        for k in sorted((k for k in kids if raw[k]["num"] is None),
                        key=lambda k: (raw[k]["std"] if raw[k]["std"] is not None else 10**6, raw[k]["name"])):
            while nxt in used:
                nxt += 1
            raw[k]["num"] = nxt
            used.add(nxt)

    # ---- D. 노드 생성 (부모 먼저) ----
    nodes: dict[tuple, SpecNode] = {}
    variants: list[dict] = []
    for kp in sorted(raw, key=lambda k: (len(k), k)):
        r = raw[kp]
        items: list[_Obs] = r["items"]
        profile = kp[0]
        depth = len(kp) - 1
        num, name = r["num"], r["name"]
        label = name if depth == 1 else fmt_child(None, name)
        total = totals[profile]
        support = sorted({o.project for o in items}, key=projects.index)
        need = math.ceil(2 * total / 3) if total else 1
        if depth == 1:
            node_id, parent_id, segment = kp[1], None, fmt_top(num, name)
            if workspace and items:
                segment = items[0].seg.original  # 실제 폴더 이름 그대로 (JOIN 대상)
            display = segment
        else:
            parent = nodes.get(_parent_kp(kp))
            if parent is None:
                continue
            prefix = "m" if (profile == "maint" and depth == 2) else parent.node_id
            node_id = f"{prefix}.{num:02d}"
            parent_id = parent.node_id
            segment = fmt_child(num, name)
            display = f"{parent.display_path}/{segment}"
        exemplars: list[str] = []
        if kp[1] != "src":
            for o in items:
                for f in _list_files(o.path, allow_read):
                    if f not in exemplars:
                        exemplars.append(f)
                    if len(exemplars) >= MAX_EXEMPLARS:
                        break
        from_std = r["std"] is not None
        adopted = len(support) >= need or from_std
        origin = "DOC_STANDARD" if (from_std and not items) else "EXTRACTED"
        desc = describe(r["key"], label, exemplars)
        if workspace and depth == 1:
            desc = _workspace_desc(segment, exemplars)
        node = SpecNode(
            node_id=node_id, parent_id=parent_id, profile=profile, segment=segment, display_path=display,
            label=label, key=r["key"], description=desc,
            role="PROTECTED" if node_id == "src" else "DEST",
            status="ADOPTED" if adopted else "CANDIDATE",
            support_count=len(support), support_total=total, support_projects=support,
            missing_in=[p for p in projects if p not in support and (profile == "common" or profiles.get(p) == profile)],
            aliases=sorted({o.seg.original for o in items}), exemplar_filenames=exemplars, origin=origin,
            hint=("기존 폴더" if workspace and depth == 1 else
                  "허브 폴더 — 내용을 기존 노드로 분산 권장" if node_id.startswith("x") and len(support) == 1
                  else "산출물목록 기재" if from_std and len(support) < need else ""),
        )
        node.required_for_handover = bool(r.get("std_required"))
        nodes[kp] = node
        variants.append({
            "node_id": node_id, "display_path": display,
            "observed": {p: next((o.seg.original for o in items if o.project == p), None) for p in projects},
        })

    # 자식이 있으면 CONTAINER, 채택된 자식의 부모는 채택
    by_id = {n.node_id: n for n in nodes.values()}
    for n in nodes.values():
        if n.parent_id and n.parent_id in by_id:
            parent = by_id[n.parent_id]
            if parent.role != "PROTECTED":
                parent.role = "CONTAINER"
            if n.status == "ADOPTED":
                parent.status = "ADOPTED"

    ordered = sorted(nodes.values(), key=lambda n: n.display_path)
    for i, n in enumerate(ordered):
        n.order = i
    return Extraction(projects, profiles, ordered, variants, dict(messy), entries, std_unmatched)


_PERSON = re.compile(r"^(.{2,4}?)\s*님?\s*(받은|보낸)\s*자료|인수인계[_\s-]*(.{2,4})$|^(.{2,4})님")
_YEAR_DIR = re.compile(r"(20\d{2})\s*년?도?")


def _workspace_desc(name: str, exemplars: list[str]) -> str:
    """업무 작업공간 폴더 설명: 주제 / 사람·출처 / 연도·보고 축을 드러낸다 (P0 관찰 #14)."""
    ex = ", ".join(exemplars[:8])
    clean = name.strip()
    if "받은" in clean or "인수인계" in clean or "님" in clean:
        base = f"'{clean}' — 특정 사람·출처에게서 받거나 인계받은 자료 모음"
    elif _YEAR_DIR.search(clean) and any(k in clean for k in ("보고", "WBS", "결산", "연말", "과제")):
        base = f"'{clean}' — 해당 연도의 과제 보고·계획 자료"
    else:
        base = f"'{clean}' 주제의 업무 자료"
    return base + (f". 기존 파일 예: {ex}" if ex else "")


def _parent_kp(kp: tuple) -> tuple | None:
    if len(kp) == 2:
        return None
    rest = kp[1:-1]
    return ("common",) + rest if len(rest) == 1 else (kp[0],) + rest


def _match_child(raw: dict, parent_kp: tuple, key: str, prefer_profile: str | None = None) -> tuple | None:
    """parent 아래에서 비교키가 같거나(우선) 서로 포함하는 자식 키 경로."""
    kids = [k for k in raw if _parent_kp(k) == parent_kp]
    if prefer_profile:
        kids.sort(key=lambda k: k[0] != prefer_profile)
    for k in kids:
        if raw[k]["key"] == key:
            return k
    for k in kids:
        rk = raw[k]["key"]
        if len(rk) >= 2 and (rk in key or key in rk):
            return k
    return None
