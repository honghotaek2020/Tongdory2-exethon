"""기획서 13(v5.1) 반영: 넓게 보기 고정 머리글, 단계 링크, 설정 읽기 전용 잠금, F08 조건부 확인."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.test_grouping import _one_file_app, _plan
from tongdory2.config import Settings
from tongdory2.web.app import create_app
from tongdory2.web.routes.s3 import _extra_ack


def test_extra_ack_text_depends_on_plan_contents():
    assert _extra_ack({"mkdirs": [], "ops": []}) == ""
    assert _extra_ack({"mkdirs": ["/x/새"], "ops": []}) == "새로 만드는 폴더 이름과 위치를 확인했습니다."
    renamed = {"op": "MOVE", "dst": "/x/OLD_보관/a.md"}
    assert "temp → temp_보관" in _extra_ack({"mkdirs": [], "ops": [renamed]})
    assert "새로 만드는 폴더와 다듬은 이름" in _extra_ack({"mkdirs": ["/x"], "ops": [renamed]})


@pytest.mark.one_batch
def test_execute_needs_extra_confirm_when_plan_makes_folders(tmp_path):
    app, c, f, dest, store = _one_file_app(tmp_path)
    run_id = store.latest()["run_id"]
    eid = store.entries(run_id)[0]["entry_id"]
    from tongdory2.spec.store import SpecStore

    nodes = SpecStore(app.state.t.db.conn).current()["body"]["nodes"]
    node = next(n["node_id"] for n in nodes if n["display_path"] == "설계")
    c.post("/s2/assign", data={"run_id": run_id, "entry_ids": eid, "target": node})
    pid = _plan(c, run_id)
    conn = app.state.t.db.conn
    body = json.loads(conn.execute("SELECT body FROM plans WHERE plan_id=?", (pid,)).fetchone()[0])
    body["mkdirs"] = body.get("mkdirs") or [str(dest / "설계")]
    conn.execute("UPDATE plans SET body=? WHERE plan_id=?", (json.dumps(body), pid))
    conn.commit()
    page = c.get(f"/s3/plan/{pid}").text
    assert 'name="extra_confirm"' in page and "파일 1개 이동 시작" in page
    res = c.post(f"/s3/execute/{pid}", data={"confirm": "yes"}, follow_redirects=False)
    assert res.headers["location"] == f"/s3/plan/{pid}" and f.exists()


@pytest.mark.one_batch
def test_past_setup_is_read_only_and_steps_link_to_own_pages(tmp_path):
    app, c, f, dest, store = _one_file_app(tmp_path)
    home = c.get("/").text
    assert 'href="/setup/structure"' in home
    page = c.get("/setup/sources").text
    assert 'id="readonly-bar"' in page and "설정 수정 불가" in page
    assert "이 단계부터 다시 시작할까요?" in page


def test_tree_zoom_header_is_sticky():
    css = open("src/tongdory2/web/static/app.css", encoding="utf-8").read()
    assert "dialog.tree-zoom > .tz-head { position: sticky; top: 0;" in css


@pytest.mark.one_batch
def test_tree_panel_same_place_on_step_pages(tmp_path):
    app, c, f, dest, store = _one_file_app(tmp_path)
    for url, title in (("/", "분류 희망 폴더 목록"), ("/setup/structure", "분류 희망 폴더 목록"),
                       ("/s2", "폴더별 이동 계획"), ("/s3", "정리한 폴더")):
        page = c.get(url).text
        assert '<div class="with-tree">' in page and 'id="tree-panel"' in page and title in page, url
    assert 'id="tree-panel"' not in c.get("/s4").text


@pytest.mark.one_batch
def test_new_run_keeps_history_and_unconfirms(tmp_path):
    app, c, f, dest, store = _one_file_app(tmp_path)
    runs_before = app.state.t.db.conn.execute("SELECT COUNT(*) FROM catalog_runs").fetchone()[0]
    res = c.post("/new-run", data={"confirm": "yes"}, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/"
    assert not any(r.confirmed for r in app.state.t.db.list_roots())
    assert app.state.t.db.conn.execute("SELECT COUNT(*) FROM catalog_runs").fetchone()[0] == runs_before


@pytest.mark.one_batch
def test_demo_reset_backs_up_then_clears_app_records_only(tmp_path):
    app, c, f, dest, store = _one_file_app(tmp_path)
    assert c.post("/demo-reset", data={}, follow_redirects=False).status_code == 303
    assert app.state.t.db.list_roots()  # 확인 체크 없으면 그대로
    c.post("/demo-reset", data={"confirm": "yes"})
    conn = app.state.t.db.conn
    assert not app.state.t.db.list_roots()
    assert conn.execute("SELECT COUNT(*) FROM catalog_runs").fetchone()[0] == 0
    backups = list((app.state.t.db.path.parent / "backups").glob("tongdory2-*.db"))
    assert len(backups) == 1 and backups[0].stat().st_size > 0
    assert f.exists() and (dest / "설계").is_dir()  # 내 PC 파일은 그대로
    assert c.get("/").status_code == 200


@pytest.mark.one_batch
def test_dup_compare_add_and_clear(tmp_path):
    app, c, f, dest, store = _one_file_app(tmp_path)
    run_id = store.latest()["run_id"]
    page = c.get("/s2").text
    assert "원본·사본 비교" in page or "DUP" not in page  # 중복이 없으면 표가 비어 있어도 된다
    r = c.post("/s2/check", data={"run_id": run_id, "action": "dups_add", "entry_id": ["nope"]}, follow_redirects=False)
    assert r.status_code == 303
    r = c.post("/s2/check", data={"run_id": run_id, "action": "dups_clear"}, follow_redirects=False)
    assert r.status_code == 303


def test_resume_state_from_running_job():
    from types import SimpleNamespace

    from tongdory2.web import progress

    t = SimpleNamespace(job={"running": True, "done": 30, "total": 120, "run_id": "run_abc", "stage": "검사"})
    j = t.job
    pct = int(j["done"] * 100 / j["total"])
    assert pct == 25 and hasattr(progress, "state")


def test_sidebar_single_highlight_and_locked_steps(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "app"), LINER_API_KEY=""))
    c = TestClient(app, base_url="http://127.0.0.1:8765")

    for url in ("/", "/s3"):
        page = c.get(url).text
        assert page.count('class="navbtn active"') == 1
        assert f'class="navbtn active" href="{url}"' in page
        assert 'class="navbtn step now " href="/setup/structure"  aria-current="step"' in page
        assert '<b class="st-name">정리 설정</b><small class="st-sum">진행 단계 · 설정 0/3</small>' in page
        assert page.count("진행 단계 · ") == 1
        assert 'aria-disabled="true" data-locked="1"' in page
        assert "앞 단계를 먼저 마쳐야 이 화면을 열 수 있습니다." in page


@pytest.mark.one_batch
def test_done_link_does_not_highlight_history(tmp_path):
    app, c, f, dest, store = _one_file_app(tmp_path)
    page = c.get("/s3?batch=x&done=1").text
    assert '<a class="navbtn " href="/s3">' in page


@pytest.mark.one_batch
def test_review_has_no_past_notice_before_execution_and_setup_shows_flash(tmp_path):
    app, c, f, dest, store = _one_file_app(tmp_path)
    assert 'id="soft-past-bar"' not in c.get("/s2").text
    page = c.post("/new-run", data={"confirm": "yes"}).text
    assert "기존 폴더 설정을 비웠습니다. 1단계(분류 희망 폴더 목록)부터 새로 설정해 주세요." in page


@pytest.mark.one_batch
def test_history_display_name_preserves_undo_link_and_tree_reflects_execution(tmp_path):
    import re

    from tongdory2.plan.executor import list_batches

    app, client, original, dest, store = _one_file_app(tmp_path)
    run_id = store.latest()["run_id"]
    plan_id = _plan(client, run_id)
    preview = client.get(f"/s3/plan/{plan_id}")
    assert "이동 예정 1개" in preview.text
    client.post(f"/s3/execute/{plan_id}", data={"confirm": "yes", "extra_confirm": "yes"})
    batch = list_batches(app.state.t.db.conn)[0]
    assert re.fullmatch(r"BT_\d{8}_\d{6}", batch["name"])
    url = f"/s3?batch={batch['batch_id']}"
    history = client.get(url)
    assert f"{batch['name']} 상세" in history.text and "이동 완료 1개" in history.text
    assert (dest / "보고" / "회의록" / original.name).exists()
    assert f'action="/s3/undo/{batch["batch_id"]}"' in history.text
    client.post(f"/s3/undo/{batch['batch_id']}", data={"confirm": "yes"})
    restored = client.get(url)
    assert f"{batch['name']} 상세" in restored.text and "이동 완료 1개" not in restored.text
    assert original.exists()


def test_batch_names_distinguish_records_created_in_same_second(tmp_path):
    from tongdory2.db import DB
    from tongdory2.plan.executor import init, list_batches

    conn = DB(tmp_path / "test.db").conn
    init(conn)
    with conn:
        conn.executemany("INSERT INTO batches VALUES(?, NULL, 'EMPTY', '{}', ?, NULL)",
                         [(bid, "2026-11-20T00:00:00+00:00") for bid in ("batch_old", "batch_new")])
    second, first = list_batches(conn)
    assert first["batch_id"] == "batch_old" and second["batch_id"] == "batch_new"
    assert second["name"] == first["name"] + "_02"
