"""K-02·A-05 지식센터 보관 — ⑤ `02.지식센터/<과제명>/<날짜>/`에 **복사**(원본 유지).

포함: 현재 과제 문서 영역(소스·.git·빌드·엉망 폴더 제외), 인수인계 README·문서 색인, 제자리 목록화 문서(Git에 없던 것 회수)
제외: 민감(L3), AI 도구 설정, 백업 사본, 링크, 소스 영역
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from tongdory2.fsx.filters import is_ai_context
from tongdory2.fsx.paths import nfc, real
from tongdory2.fsx.roots import STORAGE_KC
from tongdory2.fsx.scope import Scope
from tongdory2.fsx.sensitivity import classify_sensitivity, read_head
from tongdory2.spec.normalize import is_messy

SKIP = {".git", "node_modules", "build", "dist", "target", ".venv", "venv", "__pycache__", ".idea"}


@dataclass
class KcPlan:
    dest_root: str
    pairs: list[tuple[str, str]] = field(default_factory=list)
    mkdirs: list[str] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    recovered_not_in_git: int = 0
    in_place_count: int = 0
    inplace_destinations: set[str] = field(default_factory=set)
    recovered_destinations: set[str] = field(default_factory=set)
    meta: dict = field(default_factory=dict)


def archive_sensitive(path: Path, sensitive_paths: set[str], head: bytes | None = None) -> bool:
    return (bool(sensitive_paths and str(real(path)) in sensitive_paths)
            or classify_sensitivity(path, head).level == "L3")


def plan_copy(current_root: Path, storage_root: Path, inplace_entries: list[dict], spec_version: int,
              today: date | None = None, *, scope: Scope, sensitive_paths: set[str] | None = None) -> KcPlan:
    today = today or date.today()
    dest = storage_root / STORAGE_KC / current_root.name / today.isoformat()
    n = 1
    while dest.exists():  # 같은 날 두 번째 보관은 새 폴더 (덮어쓰기 없음)
        dest = dest.with_name(f"{today.isoformat()}_{n}")
        n += 1
    kp = KcPlan(str(dest))
    known_sensitive = sensitive_paths or set()
    dirs: set[Path] = {dest}
    anc = dest.parent
    while anc != storage_root and not anc.exists():  # 없는 상위 폴더(02.지식센터/<과제명>)
        dirs.add(anc)
        anc = anc.parent

    def add(src: Path, dst: Path) -> None:
        kp.pairs.append((str(src), str(dst)))
        d = dst.parent
        while d != dest.parent and d not in dirs:
            dirs.add(d)
            d = d.parent

    def content_allowed(path: Path) -> bool:
        reason = "민감"
        if not archive_sensitive(path, known_sensitive):
            head = read_head(path, scope)
            if head is not None and not archive_sensitive(path, known_sensitive, head):
                return True
            if head is None:
                reason = "읽을 수 없음"
        kp.excluded.append({"name": path.name, "reason": reason})
        return False

    for cur, dnames, fnames in os.walk(current_root, followlinks=False):
        cp = Path(cur)
        rel_dir = cp.relative_to(current_root)
        if rel_dir.parts and (rel_dir.parts[0].endswith("source") or rel_dir.parts[0].endswith("소스") or rel_dir.parts[0] == "src"):
            dnames[:] = []
            continue
        dnames[:] = [d for d in dnames if d not in SKIP and not is_messy(d) and not (cp / d).is_symlink()
                     and not is_ai_context((rel_dir / d).as_posix())]
        for f in sorted(fnames):
            p = cp / f
            if f.startswith(".") or p.is_symlink():
                continue
            if is_ai_context((rel_dir / f).as_posix()):
                kp.excluded.append({"name": f, "reason": "AI 설정"})
                continue
            if content_allowed(p):
                add(p, dest / "과제" / p.relative_to(current_root))

    archive_names: set[str] = set()
    for e in inplace_entries:
        reason = e.get("reason_code")
        if reason in ("AI_CONTEXT", "SENSITIVE") or e.get("sensitivity") == "L3":
            kp.excluded.append({"name": e["name"], "reason": "AI 설정" if reason == "AI_CONTEXT" else "민감"})
            continue
        if e.get("kind") != "text" or e.get("verdict") not in ("JOIN", "HOLD") or e.get("backup_of"):
            continue
        src = Path(e["path"])
        if not content_allowed(src):
            continue
        root = scope.owner(src)
        if root is None:
            continue
        dst = dest / "AI협업문서" / f"{root.path.name}_{root.root_id}" / src.relative_to(root.path)
        original_dst = dst
        number = 1
        while nfc(str(dst)).casefold() in archive_names:
            dst = original_dst.with_name(f"{original_dst.stem}_{number}{original_dst.suffix}")
            number += 1
        archive_names.add(nfc(str(dst)).casefold())
        add(src, dst)
        kp.inplace_destinations.add(str(dst))
        if e.get("git_status") in ("IGNORED", "UNTRACKED"):
            kp.recovered_destinations.add(str(dst))
    kp.in_place_count = len(kp.inplace_destinations)
    kp.recovered_not_in_git = len(kp.recovered_destinations)
    kp.mkdirs = sorted(str(d) for d in dirs)
    kp.meta = {
        "project": current_root.name, "archived_on": today.isoformat(), "spec_version": spec_version,
        "files": len(kp.pairs), "in_place_docs": kp.in_place_count, "recovered_not_in_git": kp.recovered_not_in_git,
        "excluded": len(kp.excluded), "note": "원본은 그대로 두고 복사했습니다. 민감 파일·AI 도구 설정은 제외.",
    }
    return kp


def meta_json(kp: KcPlan, copied_paths: set[str]) -> str:
    return json.dumps(kp.meta | {
        "files": len(copied_paths),
        "in_place_docs": len(kp.inplace_destinations & copied_paths),
        "recovered_not_in_git": len(kp.recovered_destinations & copied_paths),
    }, ensure_ascii=False, indent=2)
