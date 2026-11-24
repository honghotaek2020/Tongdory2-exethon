"""완료 화면은 선택한 작업의 이동·생성과 실제 폴더 존재 여부를 구분한다."""

import json
import re

from fastapi.testclient import TestClient

from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.handover.final_catalog import batch_folder_results, folder_presence
from tongdory2.plan.executor import init, save_plan
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import create_app
from tongdory2.web.setup_state import scope_key


def _app(tmp_path):
    home = tmp_path / "home"
    source = home / "Desktop"
    root = home / "Documents" / "결과"
    source.mkdir(parents=True)
    root.mkdir(parents=True)
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY=""))
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.post("/setup/structure", data={"paths": "기존폴더\n미생성기준 [필수]\n작업안한폴더"})
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    client.post("/setup/destination", data={"path": "~/Documents/결과"})
    return app.state.t, client, root


def test_done_lists_only_worked_folders_and_moved_files(tmp_path):
    t, client, root = _app(tmp_path)
    for folder in ("기존폴더", "작업안한폴더", "09조사", "빈생성폴더"):
        (root / folder).mkdir()
    (root / "기존폴더" / "이전자료.txt").write_text("기존 자료")
    (root / "작업안한폴더" / "무관한자료.txt").write_text("무관한 자료")
    (root / "기존폴더" / "옮긴자료.txt").write_text("분석 자료")
    (root / "09조사" / "국내후보.md").write_text("국내 판매처 조사")
    operations = [
        {"op": "MKDIR", "dst": str(root / "09조사"), "created_dir": 1},
        {"op": "MKDIR", "dst": str(root / "빈생성폴더"), "created_dir": 1},
        {"op": "MOVE", "dst": str(root / "기존폴더" / "옮긴자료.txt")},
        {"op": "MOVE", "dst": str(root / "09조사" / "국내후보.md")},
    ]
    for seq, op in enumerate(operations):
        op["entry_id"] = str(seq)
        op["seq"] = seq
    spec = SpecStore(t.db.conn).current()
    store = CatalogStore(t.db.conn)
    rid = store.start(spec["version"], {"scope_key": scope_key(t.db.list_roots(), spec)})
    store.finish(rid, [], 0)
    init(t.db.conn)
    pid = save_plan(t.db.conn, rid, {"total": 2, "ops": operations, "skipped": []})
    t.db.conn.execute("INSERT INTO batches VALUES(?,?,?,?,?,NULL)",
                      ("result-test", pid, "DONE", json.dumps({"moved": 2, "created_dirs": 2}), "2026-11-21T10:00:00+00:00"))
    for seq, op in enumerate(operations):
        t.db.conn.execute("INSERT INTO operations(batch_id,seq,op,dst,status,reason,created_dir) VALUES(?,?,?,?,?,?,?)",
                          ("result-test", seq, op["op"], op["dst"], "DONE", "OK", op.get("created_dir", 0)))
    t.db.conn.commit()
    response = client.get("/s3?batch=result-test&done=1")
    assert response.status_code == 200
    table = re.search(r'<section class="card" id="final-catalog-table">(.*?)</section>', response.text, re.S).group(1)
    assert "폴더별 정리 결과" in table and "3개 폴더" in table
    assert "국내후보.md" in table and "옮긴자료.txt" in table and "폴더 생성 완료" in table
    assert all(label not in table for label in ("미생성기준", "작업안한폴더", "이전자료.txt", "무관한자료.txt", "폴더 없음"))
    rows = {row["path"]: row for row in response.context["tree_rows"]}
    assert rows["미생성기준"]["presence"] == "missing" and rows["작업안한폴더"]["presence"] == "exists"
    hidden = re.search(r'<div class="trow" data-tree-path="미생성기준"[^>]*>', response.text).group(0)
    assert "display:none" in hidden
    assert rows["09조사"]["made"] and rows["빈생성폴더"]["made"]
    assert "정리한 폴더" in response.text and "이번에 파일을 옮긴 폴더" not in response.text


def test_undone_failed_and_existing_mkdir_are_not_completed_work(tmp_path):
    t, _client, root = _app(tmp_path)
    scope = t.scope()
    ops = [
        {"op": "MOVE", "dst": str(root / "실패" / "x.txt"), "status": "FAILED"},
        {"op": "MOVE", "dst": str(root / "되돌림" / "x.txt"), "status": "DONE", "undo_status": "UNDONE"},
        {"op": "MKDIR", "dst": str(root / "기존"), "status": "DONE", "created_dir": 0},
        {"op": "MOVE", "dst": str(root / "지워진폴더" / "x.txt"), "status": "DONE"},
    ]
    rows = batch_folder_results(ops, root, scope)
    assert len(rows) == 1 and rows[0]["path"] == "지워진폴더"
    assert rows[0]["presence"] == "missing" and rows[0]["moved"] == 1
    assert rows[0]["files"] == []


def test_presence_does_not_treat_out_of_scope_or_symlink_as_missing(tmp_path):
    t, _client, root = _app(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "링크").symlink_to(outside)
    assert folder_presence(outside, t.scope()) == "unavailable"
    assert folder_presence(root / "링크", t.scope()) == "unavailable"
    assert folder_presence(root / "없는폴더", t.scope()) == "missing"
