"""S2-A 제자리 목록화 (TC A-01~A-05, 02_정책 §17).

④ 역할 폴더(저장소 안 AI 협업 문서)를 **이동 없이** 목록화한다.
- AI 도구 설정 파일 → EXCLUDE AI_CONTEXT (본문 전송 없음)
- 민감 md → EXCLUDE SENSITIVE
- 문서 유형: 경로(백업·AI 설정) → 머리말 type → 제목·파일명 키워드 (로컬 규칙, API 없음)
- 가상 노드: 연결된 ② 과제 명세의 노드에 Embeddings → Rerank → Decisions (이동 없음, in_place=True)
- 백업 사본: 원본과 해시 동일 → DUP, 다르면 원본과 같은 노드 + backup_of
- Git 상태 배지: 읽기 전용 git ls-files
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections import Counter
from datetime import date
from pathlib import Path

from tongdory2.catalog.pipeline import P_NODE_MIN, TOP_K_EMBED, TOP_K_RERANK, Entry, _cos
from tongdory2.fsx import sensitivity as fx_sens
from tongdory2.fsx.filters import is_ai_context
from tongdory2.fsx.gitstatus import read_git_status, status_of
from tongdory2.fsx.mdmeta import read_md
from tongdory2.fsx.paths import tilde
from tongdory2.fsx.roots import RegisteredRoot
from tongdory2.fsx.scope import Access, Scope
from tongdory2.spec.normalize import compare_key

SKIP = {".git", "node_modules", "build", "dist", "target", ".venv", "venv", "__pycache__", ".idea"}
BACKUP_DIR = re.compile(r"^(docs?_?backup|backup|bak|99[._ ]?보관|보관|_?old|archive)$", re.I)
DOC_TYPES = [  # 순서가 우선순위
    ("AI_INSTRUCTION", ("작업 지시", "작업지시", "task_", "prompt", "프롬프트")),
    ("MEETING_HANDOVER", ("회의", "meeting", "인수인계", "handover", "미팅")),
    ("TEST", ("테스트", "시험", "test", "시나리오")),
    ("PLAN", ("계획", "plan", "마일스톤", "roadmap", "일정")),
    ("ANALYSIS", ("분석", "analysis", "요구사항", "as-is")),
    ("DESIGN", ("설계", "design", "erd", "아키텍처", "인터페이스", "architecture")),
    ("REPORT", ("보고", "report", "점검", "결과")),
]
FM_TYPE = {"plan": "PLAN", "design": "DESIGN", "analysis": "ANALYSIS", "test": "TEST", "report": "REPORT",
           "meeting": "MEETING_HANDOVER", "handover": "MEETING_HANDOVER"}
DOC_TYPE_LABEL = {"PLAN": "계획", "DESIGN": "설계", "ANALYSIS": "분석", "TEST": "시험", "REPORT": "보고",
                  "MEETING_HANDOVER": "회의·인수인계", "BACKUP": "백업", "AI_INSTRUCTION": "AI 작업 지시", "OTHER": "미분류"}
HANDOVER_NODE = {"node_id": "entry.handover", "display_path": "업무인수인계(진입점)", "segment": "업무인수인계",
                 "label": "업무인수인계", "description": "인수인계 문서: 업무 인수인계 메모, 담당 변경, 남은 일과 연락처 정리.",
                 "exemplar_filenames": [], "profile": "common"}


def _doc_type(rel: str, title: str, fm: dict) -> str:
    parts = Path(rel).parts
    if any(BACKUP_DIR.match(p) for p in parts[:-1]):
        return "BACKUP"
    t = fm.get("type", "").lower()
    if t in FM_TYPE:
        return FM_TYPE[t]
    hay = f"{Path(rel).name} {title}".lower()
    for dt, kws in DOC_TYPES:
        if any(k in hay for k in kws):
            return dt
    return "OTHER"


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_inplace(root: RegisteredRoot, scope: Scope, home: Path, today: date | None = None) -> tuple[list[Entry], dict]:
    today = today or date.today()
    git = read_git_status(root.path)
    entries: list[Entry] = []
    non_md = 0
    n = 0
    for cur, dirs, files in os.walk(root.path, followlinks=False):
        curp = Path(cur)
        keep = []
        for d in sorted(dirs):
            dp = curp / d
            if d in SKIP:
                continue
            if dp.is_symlink() or dp.is_junction():  # 링크 폴더는 따라가지 않는다
                rel = dp.relative_to(root.path).as_posix()
                e = Entry(f"a{n:04d}", str(dp), tilde(dp, home), d, "제자리 목록화", root.root_id, in_place=True)
                e.verdict, e.reason_code = "EXCLUDE", "OUT_OF_SCOPE" if not scope.link_target_in_scope(dp) else "EX_LINK"
                e.reason, e.git_status = "링크 — 따라가지 않음", status_of(rel, git)
                entries.append(e)
                n += 1
                continue
            keep.append(d)
        dirs[:] = keep
        for f in sorted(files):
            p = curp / f
            rel = p.relative_to(root.path).as_posix()
            if not f.lower().endswith(".md"):
                non_md += 1
                continue
            if not scope.check(p, Access.READ).allowed:
                continue
            e = Entry(f"a{n:04d}", str(p), tilde(p, home), f, "제자리 목록화", root.root_id, in_place=True, kind="text")
            n += 1
            st = p.lstat()
            e.size, e.mtime = st.st_size, st.st_mtime
            e.this_month = date.fromtimestamp(st.st_mtime).strftime("%Y-%m") == today.strftime("%Y-%m")
            e.git_status = status_of(rel, git)
            if is_ai_context(rel):
                e.verdict, e.reason_code, e.doc_type = "EXCLUDE", "AI_CONTEXT", "AI_INSTRUCTION"
                e.reason = "AI 도구 설정 파일 — 건드리지 않고 본문도 보내지 않음"
                e.badges.append("AI 설정")
                entries.append(e)
                continue
            meta = read_md(p)
            sens = fx_sens.classify_sensitivity(p, meta.body.encode()[:4096])
            e.sensitivity, e.signals = sens.level, list(sens.signals)
            if sens.level == "L3":
                e.verdict, e.reason_code, e.reason = "EXCLUDE", "SENSITIVE", "민감 정보 포함 — 외부 전송·지식센터 복사 제외"
                e.badges.append("민감")
                entries.append(e)
                continue
            e.title = meta.title
            e.doc_type = _doc_type(rel, meta.title, meta.frontmatter)
            body = fx_sens.mask(meta.body)[:1200] if sens.level == "L1" else ""
            e.payload = "\n".join(x for x in (f"파일명: {f}", f"제목: {meta.title}" if meta.title else "",
                                               "목차: " + ", ".join(meta.headings[:10]) if meta.headings else "", body) if x)
            e.preview = meta.body.splitlines()[:60]
            e.verdict = "PENDING"
            entries.append(e)

    # 백업 사본 ↔ 원본
    originals = {compare_key(Path(e.name).stem): e for e in entries if e.doc_type not in (None, "BACKUP") and e.verdict == "PENDING"}
    for e in entries:
        if e.doc_type != "BACKUP":
            continue
        o = originals.get(compare_key(Path(e.name).stem))
        if o is None:
            continue
        e.backup_of = o.entry_id
        e.badges.append("백업 사본")
        if _sha(Path(e.path)) == _sha(Path(o.path)):
            e.verdict, e.reason_code = "DUP", "IDENTICAL_HASH"
            e.reason = f"원본 '{o.display}'과 내용이 같음 — 이동·삭제 없음"
        else:
            e.verdict = "PENDING_BACKUP"
    for e in entries:
        if e.verdict == "PENDING" and e.doc_type in ("OTHER", "AI_INSTRUCTION"):
            e.verdict, e.reason_code = "HOLD", "NO_NODE_MATCH"
            e.reason = "분류 희망 폴더에 대응하지 않는 문서 유형 — 색인에는 포함"
            e.payload = ""
    md = [e for e in entries if e.kind == "text"]
    not_in_git = [e for e in md if e.git_status in ("IGNORED", "UNTRACKED")]
    summary = {
        "md_total": len(md), "not_in_git": len(not_in_git),
        "ignored": sum(1 for e in md if e.git_status == "IGNORED"),
        "untracked": sum(1 for e in md if e.git_status == "UNTRACKED"),
        "tracked": sum(1 for e in md if e.git_status == "TRACKED"),
        "this_month": sum(1 for e in md if e.this_month), "non_md": non_md, "is_repo": git is not None,
    }
    return entries, summary


async def map_virtual(entries: list[Entry], spec: dict, client) -> None:
    """PENDING md를 명세 노드에 가상 매핑 (이동 없음)."""
    pool = [n for n in spec["body"]["nodes"] if n["status"] == "ADOPTED" and n["role"] == "DEST"
            and n["profile"] in ("common", "build")] + [HANDOVER_NODE]
    todo = [e for e in entries if e.verdict == "PENDING"]
    if todo:
        texts = [f"{n['segment']}: {n['description']}" for n in pool]
        vecs = await client.embed(texts + [e.payload for e in todo])
        nvec, fvec = vecs[: len(pool)], vecs[len(pool):]
        sem = asyncio.Semaphore(4)

        async def one(e: Entry, v: list[float]) -> None:
            sims = sorted(((i, _cos(v, nv)) for i, nv in enumerate(nvec)), key=lambda x: -x[1])[:TOP_K_EMBED]
            async with sem:
                rr = await client.rerank(e.payload[:2000], [texts[i] for i, _ in sims], top_n=TOP_K_RERANK)
            top = [sims[i][0] for i, _ in rr]
            crit = {f"n{k}": texts[idx][:300] for k, idx in enumerate(top)}
            crit["none"] = "어느 산출물 폴더에도 해당하지 않음"
            async with sem:
                ans = await client.decide({"file": e.payload[:1200]},
                                          {"node": {"type": "choice", "instructions": "이 문서는 과제의 어느 산출물에 해당하나?", "criteria": crit}})
            a = ans.get("node", {})
            probs = {k: float(v) for k, v in a.get("probabilities", {}).items()}
            best = max(probs.items(), key=lambda x: x[1]) if probs else ("none", 0.0)
            e.confidence = best[1]
            if best[0] == "none" or best[1] < P_NODE_MIN:
                e.verdict, e.reason_code, e.reason = "HOLD", "NO_NODE_MATCH", "분류 희망 폴더에 확실히 대응하지 않음 — 색인에는 포함"
                return
            n = pool[top[int(best[0][1:])]]
            e.verdict, e.node_id, e.node_path = "JOIN", n["node_id"], n["display_path"]
            e.reason = f"'{n['label']}' 산출물에 대응 (확률 {best[1]:.2f}) — 이동하지 않음"

        await asyncio.gather(*(one(e, v) for e, v in zip(todo, fvec)))
    by_id = {e.entry_id: e for e in entries}
    for e in entries:
        if e.verdict == "DUP" and e.backup_of:
            o = by_id[e.backup_of]
            e.node_id, e.node_path = o.node_id, o.node_path
        if e.verdict == "PENDING_BACKUP":
            o = by_id[e.backup_of]
            e.verdict, e.node_id, e.node_path = (o.verdict if o.verdict == "JOIN" else "HOLD"), o.node_id, o.node_path
            e.reason = f"'{o.name}'의 이전 사본 — 같은 산출물로 묶음, 삭제하지 않음"


def doc_type_counts(entries: list[Entry]) -> Counter:
    return Counter(e.doc_type for e in entries if e.doc_type)
