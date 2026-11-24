"""미이동 사유는 파일별로 배타적으로 집계하고 두 완료 화면에서 같은 값을 보인다."""

from dataclasses import asdict
import os
import sqlite3
import time

from fastapi.testclient import TestClient

from tongdory2.catalog.pipeline import Entry
from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.plan.executor import init, list_batches, save_plan
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import create_app
from tongdory2.web.left_breakdown import for_batch
from tongdory2.web.left_breakdown import snapshot
from tongdory2.web.setup_state import scope_key


def test_23_files_one_move_and_22_reasons_on_both_screens(tmp_path):
    home = tmp_path / "home"
    source, destination = home / "Desktop", home / "Documents" / "저장"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    client = TestClient(create_app(Settings(
        TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY="",
    )), base_url="http://127.0.0.1:8765")
    client.post("/setup/structure", data={"paths": "보고"})
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    client.post("/setup/destination", data={"path": "~/Documents/저장"})
    t = client.app.state.t
    spec = SpecStore(t.db.conn).current()
    node = next(n for n in spec["body"]["nodes"] if n["display_path"] == "보고")
    entries = []
    for i, verdict in enumerate(["JOIN"] * 3 + ["HOLD"] * 13 + ["EXCLUDE"] * 7):
        path = source / f"파일{i}.txt"
        path.write_text(f"자료 {i}")
        os.utime(path, (time.time() - 86400,) * 2)
        entries.append(asdict(Entry(
            str(i), str(path), f"~/Desktop/{path.name}", path.name, "바탕화면", "source",
            verdict=verdict, node_id=node["node_id"] if verdict == "JOIN" else None,
            node_path="보고" if verdict == "JOIN" else None, default_checked=verdict == "JOIN",  # 정리 예정 3개, 계획에는 1개만
        )))
    store = CatalogStore(t.db.conn)
    rid = store.start(spec["version"], {"scope_key": scope_key(t.db.list_roots(), spec), "batch_number": 1})
    store.finish(rid, entries, 0)
    response = client.post("/s3/plan", data={"run_id": rid, "sel": "0"}, follow_redirects=False)
    pid = response.headers["location"].rsplit("/", 1)[-1]
    client.post(f"/s3/execute/{pid}", data={"confirm": "yes", "extra_confirm": "yes"})
    bid = list_batches(t.db.conn)[0]["batch_id"]
    result = for_batch(t.db.conn, bid)
    assert (result["total"], result["moved"], result["left"]) == (23, 1, 22)
    assert {i["label"]: i["count"] for i in result["items"]} == {
        "정리 예정 중 미선택": 2, "확인 필요": 13, "그대로 둠": 7,
    }
    for url in (f"/s3?batch={bid}&done=1",):  # 상황판은 요약 블록만, 세부 내역은 작업 내역 화면에
        page = client.get(url).text
        assert "이번 작업에서 옮기지 않은 22개 내역:" in page
        for text in ("정리 예정 중 미선택 2개", "확인 필요 13개", "그대로 둠 7개"):
            assert text in page
        assert "실행 중 건너뜀 0개" not in page
    for item in result["items"]:
        redirect = client.get(item["href"], follow_redirects=False)
        assert redirect.status_code == 303
        assert "tab=" in redirect.headers["location"]
    # 이후 검사 판정이 달라져도 과거 작업의 미선택 사유는 바뀌지 않는다.
    entries[1]["verdict"] = "HOLD"
    store.finish(rid, entries, 0)
    assert for_batch(t.db.conn, bid)["items"] == result["items"]


def test_file_statuses_ignore_mkdir_and_include_trash_undo_and_plan_skips():
    conn = sqlite3.connect(":memory:")
    init(conn)
    CatalogStore(conn)
    rows = [{"entry_id": str(i), "bucket": "keep" if i == 1 else "plan", "selected": True}
            for i in range(8)]
    operations = [{"seq": i + 1, "op": "TRASH" if i == 1 else "MOVE", "entry_id": str(i)}
                  for i in range(7)]
    pid = save_plan(conn, "run", {"total": 8, "ops": operations, "review_snapshot": rows,
                                   "skipped": [{"entry_id": "7", "name": "중복이름", "reason": "이름 충돌"}]})
    conn.execute("INSERT INTO batches VALUES('batch', ?, 'PARTIAL_UNDO', '{}', '', NULL)", (pid,))
    for i, status in enumerate(["DONE", "DONE", "SKIP", "DENY", "FAILED", "DONE", "PENDING"]):
        conn.execute("INSERT INTO operations(batch_id,seq,op,src,dst,status,undo_status) VALUES(?,?,?,?,?,?,?)",
                     ("batch", i + 1, operations[i]["op"], f"/{i}", f"/dest/{i}", status,
                      "UNDONE" if i == 5 else None))
    conn.execute("INSERT INTO operations(batch_id,seq,op,dst,status) VALUES('batch',99,'MKDIR','/dest','DENY')")
    result = for_batch(conn, "batch")
    assert (result["moved"], result["left"]) == (2, 6)
    assert {i["label"]: i["count"] for i in result["items"]} == {
        "계획에서 제외": 1, "실행 중 건너뜀": 1, "안전 검사로 차단": 1,
        "이동 실패": 1, "원래 위치로 되돌림": 1, "실행 결과 확인 필요": 1,
    }
    assert sum(item["count"] for item in result["items"]) == result["left"]


def test_legacy_plan_does_not_claim_historical_selection():
    conn = sqlite3.connect(":memory:")
    init(conn)
    store = CatalogStore(conn)
    rid = store.start(1, {})
    store.finish(rid, [{"entry_id": "a", "verdict": "JOIN"}], 0)
    pid = save_plan(conn, rid, {"total": 2, "ops": [], "skipped": []})
    conn.execute("INSERT INTO batches VALUES('batch', ?, 'EMPTY', '{}', '', NULL)", (pid,))
    result = for_batch(conn, "batch")
    assert result["legacy"]
    assert {i["label"]: i["count"] for i in result["items"]} == {
        "정리 예정 중 계획 미포함": 1, "이전 기록의 사유 확인 필요": 1,
    }


def test_multiple_batches_with_same_entry_id_and_previous_move():
    conn = sqlite3.connect(":memory:")
    init(conn)
    store = CatalogStore(conn)
    run_ids = [store.start(1, {"batch_number": n}) for n in (1, 2)]
    for rid in run_ids:
        store.finish(rid, [{"entry_id": "same", "verdict": "JOIN"}], 0)
    conn.execute("INSERT INTO operations(batch_id,seq,op,src,dst,status)"
                 " VALUES('old',1,'MOVE','/old','/dest/old','DONE')")
    entries = [{"entry_id": f"{rid}:same", "path": path, "verdict": "JOIN"}
               for rid, path in zip(run_ids, ("/old", "/new"))]
    body = {"total": 2, "runs": run_ids, "ops": [], "skipped": [],
            "review_snapshot": snapshot(conn, entries, set())}
    pid = save_plan(conn, run_ids[-1], body)
    conn.execute("INSERT INTO batches VALUES('batch', ?, 'EMPTY', '{}', '', NULL)", (pid,))
    result = for_batch(conn, "batch")
    assert result["left"] == 2
    assert {i["label"]: i["count"] for i in result["items"]} == {
        "정리 예정 중 미선택": 1, "이전 작업에서 이동": 1,
    }
    assert len(result["items"][0]["links"]) == 2
