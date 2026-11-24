"""실행 계획의 파일별 상태로 이번 작업에서 옮기지 않은 사유를 집계한다."""

from collections import Counter
import json
from urllib.parse import urlencode

from tongdory2.catalog.store import CatalogStore
from tongdory2.plan.executor import batch_ops, init
from tongdory2.web.progress import bucket


def snapshot(conn, entries, selected):
    """계획 이후 체크·판정이 바뀌어도 실행 당시 사유를 보존한다."""
    init(conn)
    previous = {row[0] for row in conn.execute(
        "SELECT src FROM operations WHERE op IN ('MOVE','TRASH') AND status='DONE'"
        " AND (undo_status IS NULL OR undo_status!='UNDONE')"
    )}
    return [{"entry_id": e["entry_id"], "bucket": bucket(e),
             "selected": e["entry_id"] in selected or bool(e.get("override")),
             "previously_moved": e["path"] in previous} for e in entries]


def for_batch(conn, batch_id):
    row = conn.execute(
        "SELECT p.body, p.run_id FROM batches b JOIN plans p ON p.plan_id=b.plan_id WHERE b.batch_id=?",
        (batch_id,),
    ).fetchone()
    if not row:
        return None
    body, run_id = json.loads(row[0]), row[1]
    if "total" not in body:  # 문서 색인·복사 작업은 파일 정리와 구분한다.
        return None
    entries = body.get("review_snapshot")
    legacy = entries is None
    store = CatalogStore(conn)
    run_ids = body.get("runs") or [run_id]
    if legacy:
        # 이전 기록은 당시 선택을 보존하지 않았다. 현재 체크로 미선택을 단정하지 않는다.
        prefixed = len(run_ids) > 1 or any(":" in str(o.get("entry_id", ""))
            for o in body.get("ops", []) + body.get("skipped", []))
        entries = [{"entry_id": f"{rid}:{e['entry_id']}" if prefixed else e["entry_id"],
                    "bucket": bucket(e)} for rid in run_ids for e in store.entries(rid)]
    planned = {o["entry_id"]: o for o in body.get("ops", [])
               if o["op"] in ("MOVE", "TRASH")}
    skipped = {s["entry_id"] for s in body.get("skipped", [])}
    if legacy:
        known = {entry["entry_id"] for entry in entries}
        entries.extend({"entry_id": eid, "bucket": "plan"} for eid in (planned.keys() | skipped) - known)
    actual = {o["seq"]: o for o in batch_ops(conn, batch_id) if o["op"] in ("MOVE", "TRASH")}
    counts = Counter()
    moved = 0
    for entry in entries:
        eid = entry["entry_id"]
        op = actual.get(planned[eid]["seq"]) if eid in planned else None
        if op:
            if op["undo_status"] == "UNDONE":
                reason = "undone"
            elif op["status"] == "DONE":
                moved += 1
                continue
            else:
                reason = {"SKIP": "skipped", "SKIPPED": "skipped", "DENY": "denied",
                          "DENIED": "denied", "FAILED": "failed"}.get(op["status"], "pending")
        elif entry.get("previously_moved"):
            reason = "previous"
        elif eid in skipped:
            reason = "plan_skipped"
        elif eid in planned:
            reason = "pending"
        elif legacy:
            reason = {"plan": "not_in_plan", "check": "check", "keep": "keep"}[entry["bucket"]]
        elif entry.get("selected"):
            reason = "plan_skipped"
        else:
            reason = {"plan": "unselected", "check": "check", "keep": "keep"}[entry["bucket"]]
        counts[reason] += 1
    total = max(int(body["total"]), len(entries))
    # 불완전한 이전 기록의 누락분을 미선택으로 추정하지 않는다.
    counts["unknown"] += max(total - len(entries), 0)
    record_url = f"/s3?batch={batch_id}#operation-details"
    labels = {
        "unselected": "정리 예정 중 미선택", "not_in_plan": "정리 예정 중 계획 미포함",
        "check": "확인 필요", "keep": "그대로 둠", "plan_skipped": "계획에서 제외",
        "skipped": "실행 중 건너뜀", "denied": "안전 검사로 차단", "failed": "이동 실패",
        "undone": "원래 위치로 되돌림", "previous": "이전 작업에서 이동",
        "pending": "실행 결과 확인 필요", "unknown": "이전 기록의 사유 확인 필요",
    }
    items = []
    for reason, label in labels.items():
        if not counts[reason]:
            continue
        tab = {"unselected": "plan", "not_in_plan": "plan", "check": "check", "keep": "keep"}.get(reason)
        links = []
        if tab:
            for rid in run_ids:
                run = store.get(rid)
                if run:
                    number = json.loads(run.get("notice") or "{}").get("batch_number", 1)
                    query = urlencode({"run": rid, "return_to": f"/s2?result=1&batch={number}&tab={tab}"})
                    links.append({"href": f"/s2/back?{query}", "label": f"{number}번째 묶음"})
        href = f"/s3?batch={batch_id}#plan-exclusions" if reason == "plan_skipped" else record_url
        items.append({"label": label, "count": counts[reason],
                      "href": links[0]["href"] if len(links) == 1 else href, "links": links if len(links) > 1 else []})
    plan_item = next((i for i in items if i["label"] in (labels["unselected"], labels["not_in_plan"])), None)
    return {"total": total, "moved": moved, "left": total - moved, "items": items, "legacy": legacy,
            "plan_left": counts["unselected"] + counts["not_in_plan"], "plan_href": plan_item["href"] if plan_item else "/s2?tab=plan",
            "failed": counts["failed"], "denied": counts["denied"],
            "plan_exclusions": body.get("skipped", [])}
