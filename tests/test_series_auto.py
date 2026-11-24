"""묶음 시리즈: 첫 묶음 승인 뒤 남은 묶음을 뒤에서 자동 로컬 검사, 완료 묶음은 검사 중에도 열람·체크,
계획은 완료된 묶음을 합쳐서(또는 이 묶음만), 멈춤·이어서, 검사 중 설정 변경 차단, 합친 계획 실행·되돌리기."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tongdory2.catalog import pipeline
from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.web.app import create_app
from tongdory2.web.routes import s2

pytestmark = pytest.mark.auto_series
OLD = time.time() - 86400


def wait(cond, what, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.03)
    raise AssertionError(f"timeout: {what}")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_ITEMS", 2)  # 파일 5개 → 묶음 3개 (2·2·1)
    home = tmp_path.resolve() / "home"
    desk = home / "Desktop"
    dest = home / "Documents" / "정리"
    desk.mkdir(parents=True)
    (dest / "보고" / "회의록").mkdir(parents=True)
    files = []
    for i in range(5):
        f = desk / f"회의록_{i}.txt"
        f.write_text(f"가상 회의록 {i}")
        os.utime(f, (OLD + i, OLD + i))
        files.append(f)
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(home.parent / "app"), LINER_API_KEY=""))
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.post("/setup/structure", data={"paths": "보고/회의록"})
    c.post("/setup/sources", data={"paths": "~/Desktop"})
    c.post("/setup/destination", data={"path": "~/Documents/정리"})
    return app, c, files, dest


def start_first_batch(c):
    c.get("/s2?new=1", follow_redirects=False)
    page = wait(lambda: (lambda t: t if 'name="prep_token"' in t else None)(c.get("/s2").text), "prep notice")
    token = re.search(r'name="prep_token" value="([^"]+)"', page).group(1)
    c.post("/s2/analyze", data={"prep_token": token}, follow_redirects=False)


def bar(c):
    return c.get("/s2/series.json").json()


def test_remaining_batches_checked_in_background_and_viewable(env):
    app, c, files, dest = env
    start_first_batch(c)
    b = wait(lambda: (lambda x: x if not x["running"] and x["done"] == 3 else None)(bar(c)), "3 batches done")
    assert [i["state"] for i in b["batches"]] == ["done", "done", "done"] and b["pending"] == 0
    assert [i["count"] for i in b["batches"]] == [2, 2, 1]
    p2 = c.get("/s2?batch=2&tab=plan").text
    assert "2번째 묶음" in p2 and "묶음 3/3 완료" in p2
    names = set(re.findall(r'aria-label="(회의록_\d\.txt) 이동 대상으로 선택"', p2))
    assert len(names) == 2
    all_names = set()
    for n in (1, 2, 3):
        all_names |= set(re.findall(r'aria-label="(회의록_\d\.txt) 이동 대상으로 선택"', c.get(f"/s2?batch={n}&tab=plan").text))
    assert all_names == {f.name for f in files}  # 묶음끼리 겹치지 않고 빠짐없음
    # 상황판은 완료된 묶음 전체를 합쳐서 센다
    assert "5개 분석" in c.get("/").text


def test_combined_plan_and_this_batch_only_then_execute_and_undo(env):
    app, c, files, dest = env
    start_first_batch(c)
    wait(lambda: (lambda x: x if not x["running"] and x["done"] == 3 else None)(bar(c)), "3 batches done")
    store = CatalogStore(app.state.t.db.conn)
    runs = {json.loads(r["notice"])["batch_number"]: r["run_id"] for r in store.series_runs(
        json.loads(store.latest()["notice"])["series_id"])}
    # 묶음 2의 한 파일 체크 해제 (다른 묶음 화면에서도 유지)
    p2 = c.get("/s2?batch=2&tab=plan").text
    off = re.findall(r'class="pick" data-id="([^"]+)"', p2)[0]
    res = c.post("/s2/check", data={"run_id": runs[2], "entry_id": off, "checked": "0"}).json()
    assert res["checked"] == 1
    from tongdory2.web.series import count_selected

    exact = sum(len(store.effective_selection(r)) for r in runs.values())
    assert res["checked_all"] == count_selected(store, list(runs.values())) == exact == 4  # SQL 합계 = 원래 계산
    # 이 묶음만 계획
    r = c.post("/s3/plan", data={"run_id": runs[1], "use_saved": "1", "scope": "this"}, follow_redirects=False)
    only = c.get(r.headers["location"]).text
    assert re.search(r"<b>2</b>이동할 파일", only)
    # 완료된 묶음 전체 계획 → 5개 중 체크 해제 1개 빼고 4개
    r = c.post("/s3/plan", data={"run_id": runs[3], "use_saved": "1", "scope": "all"}, follow_redirects=False)
    plan_url = r.headers["location"]
    page = c.get(plan_url).text
    assert re.search(r"<b>4</b>이동할 파일", page)
    pid = plan_url.rsplit("/", 1)[1]
    res = c.post(f"/s3/execute/{pid}", data={"confirm": "yes", "extra_confirm": "yes"}, follow_redirects=False)
    assert res.headers["location"] == "/"
    moved = sorted(p.name for p in (dest / "보고" / "회의록").iterdir())
    assert len(moved) == 4 and all(n.startswith("회의록_") for n in moved)
    assert sum(f.exists() for f in files) == 1
    batch = re.search(r"/s3/undo/([^\"]+)", c.get("/s3").text).group(1)
    c.post("/s3/undo/" + batch, data={"confirm": "yes", "extra_confirm": "yes"})
    assert all(f.exists() for f in files) and not list((dest / "보고" / "회의록").iterdir())


def test_pause_resume_and_settings_blocked_while_checking(env, monkeypatch):
    app, c, files, dest = env
    gate, entered = threading.Event(), threading.Event()
    real = s2._build_batch

    def slow(*args, **kwargs):
        if args[7] == 2:  # 두 번째 묶음에서 잠시 멈춰 있게
            entered.set()
            assert gate.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(s2, "_build_batch", slow)
    start_first_batch(c)
    assert entered.wait(5)
    b = bar(c)
    assert b["running"] and [i["state"] for i in b["batches"]] == ["done", "running", "waiting"]
    # 검사 중에도 완료된 1번 묶음은 열람 가능
    assert "묶음 1/3 완료" in c.get("/s2?batch=1&tab=plan").text
    # 검사 중에는 설정 변경 차단
    blocked = c.post("/setup/structure", data={"paths": "보고/회의록\n설계"})
    assert blocked.status_code in (409, 422) or "진행 중" in blocked.text
    # 다시 분석 요청도 막고 안내
    assert "멈춤" in c.post("/s2/rescan").text
    # 멈춤 → 지금 묶음(2)을 마치고 멈춤
    c.post("/s2/auto/pause")
    gate.set()
    b = wait(lambda: (lambda x: x if not x["running"] else None)(bar(c)), "paused")
    assert [i["state"] for i in b["batches"]] == ["done", "done", "waiting"] and b["resumable"]
    assert "이어서 분류" in c.get("/s2?batch=2").text
    # 이어서 분류 → 끝까지
    c.post("/s2/auto/resume")
    b = wait(lambda: (lambda x: x if not x["running"] and x["done"] == 3 else None)(bar(c)), "resumed")
    assert b["pending"] == 0


def test_plan_becomes_stale_when_a_batch_is_rechecked_or_settings_change(env):
    app, c, files, dest = env
    start_first_batch(c)
    wait(lambda: (lambda x: x if not x["running"] and x["done"] == 3 else None)(bar(c)), "3 batches done")
    store = CatalogStore(app.state.t.db.conn)
    last = store.latest()["run_id"]
    r = c.post("/s3/plan", data={"run_id": last, "use_saved": "1", "scope": "all"}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[1]
    c.post("/setup/structure", data={"paths": "보고/회의록\n설계"})  # 설정 변경
    res = c.post(f"/s3/execute/{pid}", data={"confirm": "yes", "extra_confirm": "yes"}, follow_redirects=False)
    assert res.headers["location"] == "/s2" and all(f.exists() for f in files)
