"""한 분석의 여러 묶음(시리즈)을 다루는 도우미.

- 묶음은 파일 단위로 나뉘어 서로 겹치지 않는다. 로컬 검사는 옮기지도 보내지도 않으므로 뒤에서 미리 돌린다.
- 화면·체크·계획은 "현재 시리즈의 완료된 묶음"만 대상으로 한다. 시리즈 = 최신 실행의 series_id(현재 설정과 일치할 때).
- 계획은 그 안에 쓴 묶음들이 모두 여전히 각 묶음의 최신 결과일 때만 실행할 수 있다(다시 검사·다시 분석하면 무효).
"""

from __future__ import annotations

import json

from tongdory2.catalog.store import CatalogStore
from tongdory2.web.setup_state import current_run


def notice_of(run: dict | None) -> dict:
    return json.loads(run["notice"] or "{}") if run else {}


def current_series(store: CatalogStore, roots, spec) -> tuple[str | None, list[dict]]:
    """(series_id, 묶음별 최신 실행들). series_id가 없는 실행(이전 형식)은 그 실행 하나를 시리즈로 본다."""
    latest = store.latest()
    if not latest:
        return None, []
    sid = notice_of(latest).get("series_id")
    return sid, (store.series_runs(sid) if sid else [latest])


def done_runs(store: CatalogStore, roots, spec) -> list[dict]:
    _, runs = current_series(store, roots, spec)
    return [r for r in runs if r["status"] == "DONE" and current_run(r, roots, spec)]


def series_run(store: CatalogStore, roots, spec, run_id: str) -> dict | None:
    """run_id가 현재 시리즈의 완료된 묶음(그 묶음의 최신 결과)이면 그 실행."""
    for r in done_runs(store, roots, spec):
        if r["run_id"] == run_id:
            return r
    return None


def view_run(store: CatalogStore, roots, spec, batch: str | None) -> dict | None:
    """화면에 보여 줄 묶음: ?batch=N이 완료됐으면 그 묶음, 아니면 가장 최근에 완료된 묶음."""
    runs = done_runs(store, roots, spec)
    if not runs:
        return None
    if batch and batch.isdigit():
        for r in runs:
            if notice_of(r).get("batch_number") == int(batch):
                return r
    return runs[-1]


def decision_fp(store: CatalogStore, run_ids: list[str]) -> str:
    """#48 계획을 만든 뒤 바뀌면 안 되는 사람·분석 결정의 지문: 직접 지정, 체크, 판정·목적지."""
    import hashlib

    if not run_ids:
        return ""
    store._sel_table()
    marks = ",".join("?" * len(run_ids))
    h = hashlib.sha256()
    for sql in (
        f"SELECT run_id, entry_id, target FROM catalog_overrides WHERE run_id IN ({marks}) ORDER BY 1, 2",
        f"SELECT run_id, entry_id, checked FROM catalog_selection WHERE run_id IN ({marks}) ORDER BY 1, 2",
        f"""SELECT run_id, entry_id, json_extract(body,'$.verdict'), json_extract(body,'$.node_path')
            FROM catalog_entries WHERE run_id IN ({marks}) ORDER BY 1, 2""",
    ):
        h.update(b"|")
        for row in store.conn.execute(sql, run_ids):
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def plan_runs_fresh(store: CatalogStore, roots, spec, plan: dict) -> bool:
    ids = plan["body"].get("runs") or [plan["run_id"]]
    current = {r["run_id"] for r in done_runs(store, roots, spec)}
    if not (bool(ids) and all(i in current for i in ids)):
        return False
    saved = plan["body"].get("decision_fp")
    # #62 지문이 없는 예전 형식 계획은 지금 결정과 같은지 확인할 수 없으므로 실행하지 않는다
    return saved is not None and saved == decision_fp(store, ids)


# ---- 묶음 요약(상황판·표시줄용) — 묶음마다 한 번 계산해 둔다 ----
# 직접 지정·이동 체크는 검토 리비전, Liner 문서 분석은 공지로 변경을 확인한다.
_SUMMARY: dict[tuple, dict] = {}


def _rev(store: CatalogStore, run: dict) -> tuple:
    return (run["run_id"], run["status"], run["notice"], run.get("cost_usd"), store.review_revision(run["run_id"]))


def run_summary(store: CatalogStore, run: dict) -> dict:
    from collections import Counter

    from tongdory2.catalog.pipeline import document_candidate
    from tongdory2.web.progress import PLAN, bucket

    key = _rev(store, run)
    hit = _SUMMARY.get(key)
    if hit is not None:
        return hit
    entries = store.entries(run["run_id"])
    selected = store.effective_selection(run["run_id"], entries)
    counts, per_override, per_path, new_paths = Counter(), Counter(), Counter(), Counter()
    verdicts = Counter()
    for e in entries:
        counts[bucket(e)] += 1
        verdicts[e["verdict"]] += 1
        ov = e.get("override")
        if e.get("sensitivity") == "L3" or ov in ("HOLD", "TRASH", "CLEAR"):
            continue
        if ov and ov not in ("HOLD", "TRASH", "CLEAR"):
            per_override[ov] += 1
        elif path := e.get("node_path"):
            picked = int(e["entry_id"] in selected)
            if e["verdict"] in ("NEW_NODE", "GROUP", "ETC"):
                new_paths[path] += picked
            elif e["verdict"] in PLAN:
                per_path[path] += picked
    out = {"total": len(entries), "counts": counts, "verdicts": verdicts, "per_override": per_override, "per_path": per_path,
           "new_paths": new_paths, "liner_candidates": sum(document_candidate(e) for e in entries)}
    if len(_SUMMARY) > 256:
        _SUMMARY.clear()
    _SUMMARY[key] = out
    return out


def count_selected(store: CatalogStore, run_ids: list[str]) -> int:
    """완료된 묶음들의 재배치 체크 수 (항목을 다 읽지 않고 SQL로)."""
    if not run_ids:
        return 0
    store._sel_table()
    marks = ",".join("?" * len(run_ids))
    return store.conn.execute(f"""
        SELECT COUNT(*) FROM catalog_entries e
        LEFT JOIN catalog_selection s ON s.run_id=e.run_id AND s.entry_id=e.entry_id
        WHERE e.run_id IN ({marks})
          AND json_extract(e.body, '$.verdict') IN ('JOIN','SPEC_NODE','NEW_NODE','GROUP','ETC','DUP')
          AND COALESCE(s.checked, CASE WHEN json_extract(e.body, '$.default_checked') THEN 1 ELSE 0 END)=1
    """, run_ids).fetchone()[0]
