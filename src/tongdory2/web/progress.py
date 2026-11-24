"""상황판·단계 표시줄 상태 계산 (화면 기획 v2.3).

사용자 단계 4개: ① 정리 설정 → ② 분석·검토 → ③ 정리 실행 → ④ 완료.
내부 S0~S4 데이터를 읽기만 한다 (파일 변경 없음).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from tongdory2.fsx.paths import tilde
from tongdory2.fsx.roots import Role
from tongdory2.web.setup_state import ROLES, pending_setup_step

PLAN = ("JOIN", "SPEC_NODE", "GROUP")  # 정리 예정 (GROUP: #47 로컬 묶음 새 폴더)
CHECK = ("NEW_NODE", "HOLD", "ETC")    # 확인 필요 (ETC: 묶이지 않은 낱개 → 기타)
APPROVABLE = ("JOIN", "SPEC_NODE", "GROUP", "NEW_NODE")  # 추천 폴더가 있어 기본 선택 여부로 탭이 갈린다
KEEP = ("DUP", "EXCLUDE")             # 그대로 둠


def local_time(iso: str | None) -> str:
    """F-4: 저장된 UTC ISO 시각 → 이 PC 시간대 'YYYY-MM-DD HH:MM'."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def bucket(e: dict) -> str:
    if e.get("override") in ("HOLD", "TRASH") or (e.get("override") and e["override"] != "CLEAR"):
        return "plan"  # 사람이 직접 지정한 항목
    v = e["verdict"]
    if v in APPROVABLE:  # 폴더를 추천받은 파일: 기본으로 옮기면 정리 예정, 사람 승인이 필요하면 확인 필요(승인 필요)
        return "plan" if e.get("default_checked", True) else "check"  # 필드가 없는 예전 기록은 정리 예정
    return "check" if v in CHECK else "keep"


def needs_approval(e: dict) -> bool:
    return e["verdict"] in APPROVABLE and not e.get("default_checked", True) and not e.get("override")


def requires_liner_selection(entry: dict) -> bool:
    """Liner가 제안한 목적지는 사용자가 선택한 항목만 실행 계획에 들어간다."""
    return (entry["verdict"] in PLAN and (entry.get("confidence") or 0) > 0
            and not entry.get("default_checked") and not entry.get("override"))


def _rate(summaries: list[dict]) -> dict:
    from tongdory2.catalog.grouping import organize_rate

    return organize_rate(sum((Counter(x.get("verdicts") or {}) for x in summaries), Counter()))


def folder_tree(paths: set[str], counts: Mapping[str, int], *, proposed: set[str], created: set[str],
                only: set[str] | None = None) -> list[dict]:
    """빈 폴더도 표시하고 상위 폴더에는 아래 파일을 합산한다."""
    rows = paths | counts.keys() | proposed | created
    totals: Counter = Counter()
    parents = set()
    for path in rows:
        parts = path.split("/")
        for depth in range(1, len(parts) + 1):
            folder = "/".join(parts[:depth])
            totals[folder] += counts.get(path, 0)
            if depth < len(parts):
                parents.add(folder)
    rows |= parents
    if only is not None:
        rows &= only
    return [{"path": p, "depth": p.count("/"), "new": p in proposed, "made": p in created,
             "n": totals[p], "leaf": p not in parents}
            for p in sorted(rows, key=lambda path: path.split("/"))]


def configured_folders(spec: dict | None) -> set[str]:
    return {n["display_path"] for n in (spec["body"]["nodes"] if spec else []) if n["status"] == "ADOPTED"
            and n["profile"] in ("common", "build") and n["role"] != "PROTECTED"}


def state(t, *, tree_paths: set[str] | None = None) -> dict:
    from tongdory2.catalog.pipeline import document_candidate
    from tongdory2.catalog.store import CatalogStore
    from tongdory2.plan.executor import list_batches
    from tongdory2.spec.store import SpecStore

    roots = t.db.list_roots()
    confirmed = bool(roots) and all(r.confirmed for r in roots if r.role in ROLES)
    cur = next((r for r in roots if r.role == Role.CURRENT), None)
    collects = [r for r in roots if r.role == Role.COLLECT]
    storage = next((r for r in roots if r.role == Role.STORAGE), None)
    spec = SpecStore(t.db.conn).current()
    dest_nodes = []
    if spec:
        dest_nodes = [n for n in spec["body"]["nodes"] if n["status"] == "ADOPTED" and n["role"] == "DEST"
                      and n["profile"] in ("common", "build")]
    from tongdory2.web.series import done_runs, run_summary

    store = CatalogStore(t.db.conn)
    series = done_runs(store, roots, spec) if spec else []  # 완료된 묶음 전체를 합쳐서 보여 준다
    valid_run = bool(series)
    run = series[-1] if series else store.latest()
    summaries = [run_summary(store, r) for r in series]  # 묶음별로 한 번 계산해 둔 요약
    notice = json.loads(run["notice"]) if valid_run and run.get("notice") else {}
    series_ids = {r["run_id"] for r in series}
    partial = bool(notice.get("truncated") or notice.get("coverage_unknown") or notice.get("total_errors")
                   or notice.get("inventory_skipped"))

    counts = sum((x["counts"] for x in summaries), Counter())
    n_entries = sum(x["total"] for x in summaries)
    # 폴더별 정리 예정 수 (트리 표시용)
    per_node: Counter = Counter()
    node_by_id = {n["node_id"]: n["display_path"] for n in (spec["body"]["nodes"] if spec else [])}
    new_paths: Counter = Counter()
    for x in summaries:
        for ov, c in x["per_override"].items():
            if ov in node_by_id:
                per_node[node_by_id[ov]] += c
        per_node.update(x["per_path"])
        new_paths.update(x["new_paths"])

    batches = list_batches(t.db.conn)
    exec_batches = [b for b in batches if not b["summary"].get("label")]
    last_exec = exec_batches[0] if exec_batches else None
    executed = bool(last_exec and valid_run and last_exec["run_id"] in series_ids
                    and (last_exec["status"] in ("DONE", "PARTIAL_UNDO")
                         or (last_exec["status"] == "EMPTY" and last_exec["summary"].get("review_only"))))
    review_only = bool(executed and last_exec["status"] == "EMPTY")
    from tongdory2.web.left_breakdown import for_batch
    left_breakdown = for_batch(t.db.conn, last_exec["batch_id"]) if executed else None
    execution_scope = None
    if executed:
        plan_row = t.db.conn.execute(
            "SELECT p.body FROM plans p JOIN batches b ON b.plan_id=p.plan_id WHERE b.batch_id=?",
            (last_exec["batch_id"],),
        ).fetchone()
        body = json.loads(plan_row[0]) if plan_row else {}
        run_ids = body.get("runs") or [last_exec["run_id"]]
        numbers = []
        for rid in run_ids:
            executed_run = store.get(rid)
            if executed_run:
                numbers.append(str(json.loads(executed_run.get("notice") or "{}").get("batch_number", 1)))
        execution_scope = {"batches": "·".join(numbers),
                           "total": left_breakdown["total"] if left_breakdown else body.get("total"),
                           "run_id": last_exec["run_id"]}

    pending_setup = pending_setup_step(roots, spec)
    setup_ok = pending_setup is None
    analyzed = setup_ok and valid_run
    n_set = {"structure": 0, "sources": 1, "destination": 2, None: 3}[pending_setup]

    s = {
        "roots": roots, "confirmed": confirmed,
        "current": cur, "current_d": tilde(cur.path, t.home) if cur else None,
        "collects": [tilde(r.path, t.home) for r in collects],
        "storage_d": tilde(storage.path, t.home) if storage else None,
        "spec": spec, "dest_nodes": dest_nodes,
        "run": run, "partial": partial, "total": n_entries, "counts": counts, "notice": notice,
        "liner_candidates": sum(x["liner_candidates"] for x in summaries),
        "per_node": per_node, "new_paths": new_paths,
        "rate": _rate(summaries),
        "last_exec": last_exec, "executed": executed, "review_only": review_only,
        "left_breakdown": left_breakdown,
        "execution_scope": execution_scope,
        "latest_total": summaries[-1]["total"] if summaries else 0,
        "setup_ok": setup_ok, "analyzed": analyzed, "n_set": n_set, "pending_setup": pending_setup,
        "running": bool(getattr(t, "job", {}).get("running") or getattr(t, "prep_job", {}).get("running")),
    }
    # R07 새로고침·중단 뒤에도 검사 진행률과 작업 ID를 바로 이어서 볼 수 있게 한다
    s["resume"] = None
    for name in ("job", "prep_job", "auto_job"):
        j = getattr(t, name, None) or {}
        if not j or not (j.get("running") or (name == "auto_job" and j.get("reason") and j.get("done", 0) < j.get("total", 0))):
            continue
        total = j.get("total") or 0
        pct = int(j.get("done", 0) * 100 / total) if total else 0
        job_id = j.get("run_id") or j.get("series_id") or (j.get("token") or "")[:10] or name
        s["resume"] = {"pct": pct, "id": job_id, "running": bool(j.get("running")), "stage": j.get("stage") or ""}
        break
    # 단계 표시줄
    if not setup_ok:
        cur_step = 0
    elif not analyzed:
        cur_step = 1
    elif not executed:
        cur_step = 1 if counts.get("check") or not n_entries else 2
    else:
        cur_step = 3
    sm_exec = ""
    if executed:
        sm = last_exec["summary"]
        sm_exec = ("파일 이동 없이 검토 완료" if review_only
                   else f"이동 {sm.get('moved', 0)} · 폴더 {sm.get('created_dirs', 0)}")
    last_plan = t.db.conn.execute("SELECT plan_id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone() if analyzed else None
    s["steps"] = [
        {"name": "정리 설정", "href": f"/setup/{pending_setup or 'structure'}", "summary": f"설정 {n_set}/3" + (" · 확정" if setup_ok else "")},
        {"name": "분석·검토", "href": "/s2" if setup_ok else "/",
         "summary": (f"{n_entries}개 분석 · 확인 {counts.get('check', 0)}" if analyzed else "분석 전")},
        {"name": "정리 실행", "href": (f"/s3/plan/{last_plan[0]}" if last_plan else "/s2") if analyzed else "/", "summary": sm_exec or "실행 전"},
        {"name": "완료", "href": (f"/s3?batch={last_exec['batch_id']}&done=1" if executed else "/"),
         "summary": ("일부 되돌림" if last_exec["status"] == "PARTIAL_UNDO"
                     else "부분 조사" if partial
                     else f"분류 묶음 {execution_scope['batches']} 처리 완료" if execution_scope["batches"]
                     else "처리 완료") if executed else ""},
    ]
    for i, st in enumerate(s["steps"]):
        st["state"] = "done" if i < cur_step or (i == 3 and executed) else "now" if i == cur_step else "next"
    s["cur_step"] = cur_step

    # 트리 (채택 노드 전체 — 상위 폴더도 보여 줘야 위치가 읽힌다. 새 폴더 제안 포함)
    paths = configured_folders(spec)
    suggested = set()
    for p_new in new_paths:
        parts = p_new.split("/")
        for depth_i in range(1, len(parts) + 1):
            suggested.add("/".join(parts[:depth_i]))
    suggested -= paths
    cur_root = Path(cur.path) if cur else None
    # 0개인 제안도 목록에는 남기되 파일 수에는 더하지 않는다.
    created = {p for p in suggested if (tree_paths is None or p in tree_paths)
               and cur_root is not None and (cur_root / p).is_dir()}
    counts_by_folder = Counter({p: per_node[p] + new_paths[p] for p in per_node.keys() | new_paths.keys()})
    s["tree"] = folder_tree(paths, counts_by_folder, proposed=suggested - created, created=created, only=tree_paths)
    return s


def kst_filter(v):  # Jinja 필터 이름은 'localtime'
    return local_time(v)


__all__ = ["state", "local_time", "bucket", "Path"]
