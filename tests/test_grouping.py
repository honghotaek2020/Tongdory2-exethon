"""#47 기준 폴더에 안 맞은 파일을 원래 폴더 → 파일 이름 → 기타 순으로 묶고, 겹치는 폴더는 (1)로."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tongdory2.catalog.grouping import apply_groups, name_prefix, organize_rate
from tongdory2.catalog.pipeline import Entry
from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.web.app import create_app


def _pair(rel: str, **kw):
    p = Path("/src") / rel
    e = Entry(entry_id=rel, path=str(p), display=rel, name=p.name, source="", root_id="r1",
              reason_code=kw.pop("reason_code", "LOCAL_NO_MATCH"), **kw)
    return e, SimpleNamespace(rel=rel, root_id="r1", path=p, is_dir=False, is_link=False)


def _run(pairs, dest: Path, conn=None, nodes=()):
    entries, items = zip(*pairs)
    return list(entries), apply_groups(list(entries), list(items), dest, list(nodes), conn)


def test_name_prefix_skips_dates_numbers_versions():
    assert name_prefix("결과쿼리_01.sql") == "결과쿼리"
    assert name_prefix("20261020_요약_서버.md") == "요약"
    assert name_prefix("rsp001.json") == "rsp"
    assert name_prefix("v2_최종_회의록.docx") == "회의록"
    assert name_prefix("1.txt") is None


def test_folder_then_name_then_etc(tmp_path):
    pairs = [_pair(f"E2E/03_시험/run_{i}.md") for i in range(6)]               # 원래 폴더 6개
    pairs += [_pair(f"E2E/03_시험/sub{i}/a{i}.md") for i in range(3)]          # 하위 폴더마다 1개 → 위 폴더로 합침
    pairs += [_pair(f"잡동사니{i}/결과쿼리_{i}.sql") for i in range(5)]           # 폴더는 흩어졌지만 이름이 같음
    pairs += [_pair("홀로.txt"), _pair("x/하나.txt")]                          # 낱개
    pairs += [_pair("보안.key", sensitivity="L3"), _pair("모호.md", reason_code="LOCAL_AMBIGUOUS")]
    entries, st = _run(pairs, tmp_path)
    by = {e.entry_id: e for e in entries}
    assert {by[f"E2E/03_시험/run_{i}.md"].node_path for i in range(6)} == {"자동분류/E2E/03_시험"}
    assert by["E2E/03_시험/sub1/a1.md"].node_path == "자동분류/E2E/03_시험"  # 모자란 하위 폴더는 한 단계 위로 (이름이 다르면 한 폴더에)
    assert {by[f"잡동사니{i}/결과쿼리_{i}.sql"].node_path for i in range(5)} == {"자동분류/결과쿼리"}
    assert by["홀로.txt"].verdict == by["x/하나.txt"].verdict == "ETC"
    assert by["홀로.txt"].node_path == "자동분류/기타" and not by["홀로.txt"].default_checked
    assert by["보안.key"].verdict == "HOLD" and by["모호.md"].verdict == "HOLD"  # 민감·후보 있는 파일은 묶지 않음
    assert st == {"folder": 9, "name": 5, "etc": 2, "groups": 2}
    assert by["E2E/03_시험/run_0.md"].default_checked and by["E2E/03_시험/run_0.md"].verdict == "GROUP"


def test_existing_folder_gets_number_and_mapping_is_stable(tmp_path):
    dest = tmp_path / "dest"
    (dest / "자동분류" / "design").mkdir(parents=True)       # 이미 있는 폴더 → design (1)
    (dest / "자동분류" / "design (1)").mkdir()               # 그것도 있으면 → design (2)
    conn = sqlite3.connect(":memory:")
    entries, _ = _run([_pair(f"design/d{i}.png", reason_code="IMAGE_NOT_ANALYZED") for i in range(5)], dest, conn)
    assert {e.node_path for e in entries} == {"자동분류/design (2)"}
    # 실행으로 폴더가 생긴 뒤 다음 묶음에서도 같은 폴더를 쓴다 ((3)으로 갈라지지 않음)
    (dest / "자동분류" / "design (2)").mkdir()
    entries, _ = _run([_pair(f"design/e{i}.png", reason_code="IMAGE_NOT_ANALYZED") for i in range(5)], dest, conn)
    assert {e.node_path for e in entries} == {"자동분류/design (2)"}
    # 기준 폴더 경로와 겹쳐도 번호
    entries, _ = _run([_pair(f"보고/r{i}.md") for i in range(5)], dest, None,
                      [{"status": "ADOPTED", "display_path": "자동분류/보고"}])
    assert {e.node_path for e in entries} == {"자동분류/보고 (1)"}


def test_backup_folder_goes_to_backup_group(tmp_path):
    entries, _ = _run([_pair(f"docs_backup/b{i}.md") for i in range(5)], tmp_path)
    assert {e.node_path for e in entries} == {"자동분류/백업 사본/docs_backup"}


def test_organize_rate_excludes_intentional_keep():
    r = organize_rate(Counter(JOIN=10, GROUP=70, ETC=10, HOLD=10, EXCLUDE=50))
    assert (r["pct"], r["with_etc"], r["target"]) == (80, 90, 100)


@pytest.mark.one_batch
def test_group_card_approve_then_plan_and_execute_into_numbered_folder(tmp_path):
    home = tmp_path.resolve() / "home"
    desk = home / "Desktop"
    old = time.time() - 86400
    for i in range(5):
        f = desk / "성능시험" / f"측정_{i}.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"가상 측정 {i}")
        os.utime(f, (old, old))
    lone = desk / "홀로.txt"
    lone.write_text("가상")
    os.utime(lone, (old, old))
    dest = home / "Documents" / "정리"
    (dest / "보고" / "회의록").mkdir(parents=True)
    (dest / "자동분류" / "성능시험").mkdir(parents=True)  # 이미 있는 폴더 → (1)
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(home.parent / "app"), LINER_API_KEY=""))
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.post("/setup/structure", data={"paths": "보고/회의록"})
    c.post("/setup/sources", data={"paths": "~/Desktop"})
    c.post("/setup/destination", data={"path": "~/Documents/정리"})
    c.get("/s2?new=1")
    for _ in range(200):
        page = c.get("/s2").text
        if 'name="prep_token"' in page:
            break
        time.sleep(0.03)
    c.post("/s2/analyze", data={"prep_token": re.search(r'name="prep_token" value="([^"]+)"', page).group(1)})
    for _ in range(200):
        page = c.get("/s2?tab=plan").text
        if "전체" in page and "확인 필요" in page:
            break
        time.sleep(0.03)
    store = CatalogStore(app.state.t.db.conn)
    for _ in range(200):
        if store.latest() and store.latest()["status"] == "DONE":
            break
        time.sleep(0.03)
    run_id = store.latest()["run_id"]
    entries = store.entries(run_id)
    # 방안 B: 기준 폴더에 맞지 않은 파일은 임의 '자동분류/...' 폴더를 만들지 않고 확인 필요(HOLD)에 남긴다.
    assert "자동분류/성능시험 (1)" not in c.get("/s2?tab=plan").text
    assert all(e["verdict"] == "HOLD" and not e["node_path"] for e in entries)
    assert len(store.effective_selection(run_id)) == 0
    check = c.get("/s2?tab=check").text
    assert "확인 필요" in check
    r = c.post("/s3/plan", data={"run_id": run_id, "use_saved": "1", "scope": "all"}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[1]
    c.post(f"/s3/execute/{pid}", data={"allow_empty": "1", "confirm": "yes", "extra_confirm": "yes"})
    assert lone.exists() and all((desk / "성능시험" / f"측정_{i}.md").exists() for i in range(5))
    assert not list((dest / "자동분류" / "성능시험").iterdir())  # 기존 폴더는 건드리지 않음


# ---------------------------------------------------------------- #48 · #49

def _one_file_app(tmp_path):
    home = tmp_path.resolve() / "home"
    f = home / "Desktop" / "회의록_1.txt"
    f.parent.mkdir(parents=True)
    f.write_text("가상 회의록")
    old = time.time() - 86400
    os.utime(f, (old, old))
    dest = home / "Documents" / "정리"
    (dest / "보고" / "회의록").mkdir(parents=True)
    (dest / "설계").mkdir(parents=True)
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(home.parent / "app"), LINER_API_KEY=""))
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.post("/setup/structure", data={"paths": "보고/회의록\n설계"})
    c.post("/setup/sources", data={"paths": "~/Desktop"})
    c.post("/setup/destination", data={"path": "~/Documents/정리"})
    c.get("/s2?new=1")
    for _ in range(200):
        page = c.get("/s2").text
        if 'name="prep_token"' in page:
            break
        time.sleep(0.03)
    c.post("/s2/analyze", data={"prep_token": re.search(r'name="prep_token" value="([^"]+)"', page).group(1)})
    store = CatalogStore(app.state.t.db.conn)
    for _ in range(200):
        if store.latest() and store.latest()["status"] == "DONE":
            break
        time.sleep(0.03)
    return app, c, f, dest, store


def _plan(c, run_id):
    r = c.post("/s3/plan", data={"run_id": run_id, "use_saved": "1", "scope": "all"}, follow_redirects=False)
    return r.headers["location"].rsplit("/", 1)[1]


@pytest.mark.one_batch
def test_plan_is_stale_after_override_or_check_changes(tmp_path):
    app, c, f, dest, store = _one_file_app(tmp_path)
    run_id = store.latest()["run_id"]
    eid = store.entries(run_id)[0]["entry_id"]
    from tongdory2.spec.store import SpecStore

    nodes = SpecStore(app.state.t.db.conn).current()["body"]["nodes"]
    node = next(n["node_id"] for n in nodes if n["display_path"] == "설계")
    c.post("/s2/assign", data={"run_id": run_id, "entry_ids": eid, "target": node})
    pid = _plan(c, run_id)
    assert "이동 시작</button>" in c.get(f"/s3/plan/{pid}").text
    c.post("/s2/assign", data={"run_id": run_id, "entry_ids": eid, "target": "CLEAR"})  # 지정 취소
    page = c.get(f"/s3/plan/{pid}").text
    assert "이 계획은 실행할 수 없습니다" in page and "이동 시작</button>" not in page
    res = c.post(f"/s3/execute/{pid}", data={"confirm": "yes", "extra_confirm": "yes"}, follow_redirects=False)
    assert res.headers["location"] == "/s2" and f.exists() and not (dest / "설계" / f.name).exists()
    # 체크만 바꿔도 이전 계획은 실행되지 않는다
    pid = _plan(c, run_id)
    c.post("/s2/check", data={"run_id": run_id, "entry_id": eid, "checked": "0"})
    c.post(f"/s3/execute/{pid}", data={"confirm": "yes", "extra_confirm": "yes"})
    assert f.exists()


@pytest.mark.one_batch
def test_source_unlink_failure_leaves_single_path_and_undo_cleans_both(tmp_path, monkeypatch):
    from tongdory2.plan import executor

    app, c, f, dest, store = _one_file_app(tmp_path)
    target = dest / "보고" / "회의록" / f.name
    real_unlink = os.unlink
    mode = {"fail_src": True, "fail_dst": False}

    def flaky(p, *a, **k):
        if mode["fail_src"] and Path(p) == f:
            raise PermissionError("가상 권한 오류")
        if mode["fail_dst"] and Path(p) == target:
            raise PermissionError("가상 권한 오류")
        return real_unlink(p, *a, **k)

    monkeypatch.setattr(executor.os, "unlink", flaky)
    run_id = store.latest()["run_id"]
    c.post(f"/s3/execute/{_plan(c, run_id)}", data={"confirm": "yes", "extra_confirm": "yes"})
    assert f.exists() and not target.exists()  # 목적지 링크를 거둬 원본 한 곳만 남음
    # 목적지 회수도 실패하면 두 경로가 남고, 되돌리기가 목적지 링크만 정리한다
    mode["fail_dst"] = True
    c.post(f"/s3/execute/{_plan(c, run_id)}", data={"confirm": "yes", "extra_confirm": "yes"})
    assert f.exists() and target.exists()
    batch = re.search(r"/s3/undo/([^\"]+)", c.get("/s3").text).group(1)
    mode.update(fail_src=False, fail_dst=False)
    c.post("/s3/undo/" + batch, data={"confirm": "yes", "extra_confirm": "yes"})
    assert f.exists() and not target.exists() and f.read_text() == "가상 회의록"


# ---------------------------------------------------------------- 동작 흐름 검증 문서 3~12번

@pytest.mark.one_batch
def test_return_links_keep_place_and_moved_file_points_to_history(tmp_path):
    app, c, f, dest, store = _one_file_app(tmp_path)
    run_id = store.latest()["run_id"]
    eid = store.entries(run_id)[0]["entry_id"]
    page = c.get("/s2?result=1&batch=1&tab=keep&page=1").text
    assert 'href="?result=1&batch=1&tab=plan"' in page                     # 5: 탭 링크가 result=1 유지
    for url in ("/s2/standards", f"/s2/view/{run_id}/{eid}"):
        assert "/s2/back" in c.get(url).text                                # 3: 복귀는 마지막 결과 화면으로
    back = c.get("/s2/back?to=JOIN", follow_redirects=False).headers["location"]
    assert back == "/s2?result=1&batch=1&tab=keep&page=1#JOIN"
    assert c.get("/s2/back?to=<x>", follow_redirects=False).headers["location"].endswith("page=1")  # 위치값 검증
    # 7: 실행으로 옮긴 파일의 미리보기 → 작업 기록 안내
    pid = _plan(c, run_id)
    c.post(f"/s3/execute/{pid}", data={"confirm": "yes", "extra_confirm": "yes"})
    moved = c.get(f"/s2/view/{run_id}/{eid}")
    assert moved.status_code == 404 and "작업 기록·되돌리기" in moved.text and "/s3?batch=batch" in moved.text
    # 11: 없는 작업 기록 번호
    assert "찾을 수 없습니다" in c.get("/s3?batch=nope").text


@pytest.mark.one_batch
def test_assigned_file_is_listed_in_plan_tab(tmp_path):
    app, c, f, dest, store = _one_file_app(tmp_path)
    run_id = store.latest()["run_id"]
    eid = store.entries(run_id)[0]["entry_id"]
    from tongdory2.spec.store import SpecStore

    node = next(n["node_id"] for n in SpecStore(app.state.t.db.conn).current()["body"]["nodes"] if n["display_path"] == "설계")
    r = c.post("/s2/assign", data={"run_id": run_id, "entry_ids": eid, "target": node}, follow_redirects=False,
               headers={"referer": "http://127.0.0.1:8765/s2?result=1&tab=check"})
    assert "#shots" not in r.headers["location"]                             # 8: 스크린샷 폼이 아니면 #shots 없음
    plan = c.get("/s2?result=1&tab=plan").text
    assert "직접 지정한 파일" in plan and 'data-tab-panel="plan"' in plan.split("직접 지정한 파일")[0].rsplit("<section", 1)[1]


def test_setup_conflict_keeps_typed_input(tmp_path):
    home = tmp_path.resolve() / "home"
    (home / "Desktop").mkdir(parents=True)
    c = TestClient(create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "d"), LINER_API_KEY="")),
                   base_url="http://127.0.0.1:8765")
    c.post("/setup/structure", data={"paths": "보고/회의록"})
    r = c.post("/setup/structure", data={"paths": "보고/회의록\n방금입력", "revision": "old-revision"})
    assert r.status_code == 422 and "다른 화면" in r.text and "방금입력" in r.text  # 12: 충돌이어도 입력이 남는다


# ---------------------------------------------------------------- #60 · #62

def test_inplace_analysis_shows_payload_and_needs_approval(tmp_path, monkeypatch):
    from tongdory2.fsx.roots import Role
    from tongdory2.liner import client as liner

    home = tmp_path.resolve() / "home"
    docs = home / "Documents" / "협업과제"
    docs.mkdir(parents=True)
    for name in ("설계_메모.md", "회의_정리.md"):
        (docs / name).write_text(f"# {name}\n가상 협업 문서 본문")
    (home / "Desktop").mkdir()
    (home / "Documents" / "정리").mkdir()
    calls = []

    class Fake:
        def __init__(self, *a, **k):
            self.total_cost_usd = 0.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def embed(self, texts):
            calls.append("embed")
            return [[1.0, 0.0] for _ in texts]

        async def rerank(self, q, docs, top_n=3):
            calls.append("rerank")
            return [(0, 0.9)]

        async def decide(self, *a, **k):
            calls.append("decide")
            return {}

    monkeypatch.setattr(liner, "LinerClient", Fake)
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "d"), LINER_API_KEY="test-only-key"))
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.post("/setup/structure", data={"paths": "보고/회의록"})
    c.post("/setup/sources", data={"paths": "~/Desktop"})
    c.post("/setup/destination", data={"path": "~/Documents/정리"})
    t = app.state.t
    t.db.add_root(docs, Role.IN_PLACE, "PASTE", [], [])
    t.db.confirm_all()

    def finished():
        for _ in range(200):
            if not getattr(t, "ip_job", {}).get("running"):
                return
            time.sleep(0.03)

    page = c.post("/s2a/analyze").text
    assert "Liner에 보낼 내용 확인" in page and "가상 협업 문서 본문" in page and calls == []  # 보내기 전 미리보기
    token = re.search(r'name="token" value="([^"]+)"', page).group(1)
    approval = re.search(r'name="approval" value="([^"]+)"', page).group(1)
    c.post("/s2a/approve", data={"token": token, "approval": approval})               # 확인 체크 없음 → 차단
    assert calls == [] and getattr(t, "ip_pending", None)
    c.post("/s2a/approve", data={"token": token, "approval": "forged", "reviewed": "yes"})  # 서명 위조 → 차단
    assert calls == []
    c.post("/s2a/approve", data={"token": token, "local_only": "1"})                 # 보내지 않고 로컬만
    finished()
    assert calls == []
    page = c.post("/s2a/analyze").text
    token = re.search(r'name="token" value="([^"]+)"', page).group(1)
    approval = re.search(r'name="approval" value="([^"]+)"', page).group(1)
    c.post("/s2a/approve", data={"token": token, "approval": approval, "reviewed": "yes"})
    finished()
    assert "embed" in calls                                                           # 승인 뒤에만 전송


@pytest.mark.one_batch
def test_old_format_plan_without_fingerprint_is_blocked(tmp_path):
    import json as _json

    app, c, f, dest, store = _one_file_app(tmp_path)
    run_id = store.latest()["run_id"]
    pid = _plan(c, run_id)
    conn = app.state.t.db.conn
    body = _json.loads(conn.execute("SELECT body FROM plans WHERE plan_id=?", (pid,)).fetchone()[0])
    body.pop("decision_fp")
    with conn:
        conn.execute("UPDATE plans SET body=? WHERE plan_id=?", (_json.dumps(body, ensure_ascii=False), pid))
    assert "이동 시작</button>" not in c.get(f"/s3/plan/{pid}").text
    c.post(f"/s3/execute/{pid}", data={"confirm": "yes", "extra_confirm": "yes"})
    assert f.exists()


def test_s0_tilde_uses_configured_home(tmp_path):
    from tongdory2.fsx.roots import Role, check_root

    home = tmp_path.resolve() / "home"
    (home / "Documents" / "협업").mkdir(parents=True)
    r = check_root("~/Documents/협업", Role.IN_PLACE, [], home, tmp_path / "app")
    assert r.realpath == home / "Documents" / "협업"  # #63 실제 홈이 아니라 설정한 홈


# ---------------------------------------------------------------- #64~#67

def _inplace_app(tmp_path, monkeypatch):
    from tongdory2.fsx.roots import Role
    from tongdory2.liner import client as liner

    home = tmp_path.resolve() / "home"
    docs = home / "Documents" / "협업과제"
    docs.mkdir(parents=True)
    (docs / "설계_메모.md").write_text("# 설계\n가상 협업 문서 본문")
    (home / "Desktop").mkdir()
    (home / "Documents" / "정리").mkdir()
    calls = []

    class Fake:
        total_cost_usd = 0.0

        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def embed(self, texts):
            calls.append("embed")
            return [[1.0, 0.0] for _ in texts]

        async def rerank(self, q, docs, top_n=3):
            return [(0, 0.9)]

        async def decide(self, *a, **k):
            return {}

    monkeypatch.setattr(liner, "LinerClient", Fake)
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "d"), LINER_API_KEY="test-only-key"))
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.post("/setup/structure", data={"paths": "보고/회의록"})
    c.post("/setup/sources", data={"paths": "~/Desktop"})
    c.post("/setup/destination", data={"path": "~/Documents/정리"})
    t = app.state.t
    root = t.db.add_root(docs, Role.IN_PLACE, "PASTE", [], [])
    t.db.confirm_all()
    return app, c, t, root, calls


def _preview(c):
    page = c.post("/s2a/analyze").text
    return (re.search(r'name="token" value="([^"]+)"', page).group(1),
            re.search(r'name="approval" value="([^"]+)"', page).group(1))


def test_cancelled_preview_cannot_be_approved(tmp_path, monkeypatch):
    app, c, t, root, calls = _inplace_app(tmp_path, monkeypatch)
    token, approval = _preview(c)
    assert "취소했습니다" in c.post("/s2a/cancel", data={"token": token}).text            # #64
    c.post("/s2a/approve", data={"token": token, "approval": approval, "reviewed": "yes"})
    time.sleep(0.2)
    assert calls == []


def test_unregistered_inplace_root_blocks_old_preview_and_index(tmp_path, monkeypatch):
    app, c, t, root, calls = _inplace_app(tmp_path, monkeypatch)
    token, approval = _preview(c)
    assert c.post("/s2a/targets", data={"paths": ""}).status_code == 200
    c.post("/s2a/approve", data={"token": token, "approval": approval, "reviewed": "yes"})  # #65
    time.sleep(0.2)
    assert calls == []
    # #66 이전 결과가 있어도 대상이 없으면 색인은 500 대신 안내
    from tongdory2.catalog.store import CatalogStore as CS
    from tongdory2.spec.store import SpecStore

    CS(t.db.conn).finish(CS(t.db.conn).start(SpecStore(t.db.conn).current()["version"], {}, kind="IN_PLACE"), [], 0.0)
    page = c.get("/s2a").text
    assert "문서 색인 만들기 (README" not in page
    r = c.post("/s2a/index")
    assert r.status_code == 200 and "색인을 만들 수 없습니다" in r.text


@pytest.mark.one_batch
def test_detail_back_without_tab_history_opens_files_own_batch(tmp_path):
    app, c, f, dest, store = _one_file_app(tmp_path)
    run_id = store.latest()["run_id"]
    app.state.t.s2_back = "/s2?result=1&batch=7&tab=check"  # 다른 탭이 마지막으로 본 (가짜) 묶음
    r = c.get(f"/s2/back?to=JOIN&run={run_id}", follow_redirects=False)                # #67
    assert r.headers["location"] == "/s2?result=1&batch=1#JOIN"
    r = c.get("/s2/back?to=JOIN&run=run_unknown", follow_redirects=False)
    assert r.headers["location"] == "/s2?result=1#JOIN"


def test_same_names_split_by_parent_and_messy_names_renamed(tmp_path):
    pairs = [_pair(f"E2E/03_시험/회차{i}/요약.md") for i in range(6)]          # 같은 이름 6개가 한 묶음으로
    pairs += [_pair(f"결과서/OLD/v{i}.pptx", reason_code="LOCAL_UNSUPPORTED") for i in range(5)]
    entries, _ = _run(pairs, tmp_path)
    dests = [(e.node_path, e.name) for e in entries if e.name == "요약.md"]
    assert len(set(dests)) == 6 and all(p.startswith("자동분류/E2E/03_시험/회차") for p, _ in dests)  # 부딪치지 않음
    old = {e.node_path for e in entries if e.name.endswith(".pptx")}
    assert old == {"자동분류/백업 사본/결과서/OLD_보관"}  # 'OLD'는 실행 안전 검사가 막으므로 보관 이름으로


@pytest.mark.one_batch
def test_grouped_files_with_same_name_all_move(tmp_path):
    home = tmp_path.resolve() / "home"
    old = time.time() - 86400
    for i in range(5):
        f = home / "Desktop" / "성능" / f"회차{i}" / "요약.md"
        f.parent.mkdir(parents=True)
        f.write_text(f"가상 요약 {i}")
        os.utime(f, (old, old))
    for i in range(5):
        f = home / "Desktop" / "결과서" / "OLD" / f"v{i}.pptx"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"PK" + bytes([i]) * 10)
        os.utime(f, (old, old))
    dest = home / "Documents" / "정리"
    (dest / "보고" / "회의록").mkdir(parents=True)
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(home.parent / "app"), LINER_API_KEY=""))
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.post("/setup/structure", data={"paths": "보고/회의록"})
    c.post("/setup/sources", data={"paths": "~/Desktop"})
    c.post("/setup/destination", data={"path": "~/Documents/정리"})
    c.get("/s2?new=1")
    for _ in range(200):
        page = c.get("/s2").text
        if 'name="prep_token"' in page:
            break
        time.sleep(0.03)
    c.post("/s2/analyze", data={"prep_token": re.search(r'name="prep_token" value="([^"]+)"', page).group(1)})
    store = CatalogStore(app.state.t.db.conn)
    for _ in range(200):
        if store.latest() and store.latest()["status"] == "DONE":
            break
        time.sleep(0.03)
    run_id = store.latest()["run_id"]
    entries = store.entries(run_id)
    # 방안 B: 기준 폴더에 맞지 않으면 자동 묶기 없이 HOLD 상태 유지
    assert all(e["verdict"] == "HOLD" for e in entries)
    assert len(store.effective_selection(run_id)) == 0
    from tongdory2.spec.store import SpecStore

    node = next(n["node_id"] for n in SpecStore(app.state.t.db.conn).current()["body"]["nodes"]
                if n["display_path"] == "보고/회의록")
    c.post("/s2/assign", data={"run_id": run_id, "entry_ids": [e["entry_id"] for e in entries], "target": node})
    c.post(f"/s3/execute/{_plan(c, run_id)}", data={"confirm": "yes", "extra_confirm": "yes"})
    moved = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert len(moved) == 10, moved                                   # 같은 이름 5개·OLD 폴더 5개 모두 이동
    assert not any(p.exists() for p in (home / "Desktop").rglob("*") if p.is_file())


def test_move_and_undo_keep_original_unicode_form(tmp_path):
    """#71 macOS NFD 이름은 옮긴 뒤에도, 되돌린 뒤에도 같은 바이트 이름이어야 한다."""
    import unicodedata

    from tongdory2.plan import executor

    nfd = unicodedata.normalize("NFD", "결과서레이아웃예시1.pptx")
    src_dir, dst_dir = tmp_path / "src", tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    (src_dir / nfd).write_bytes(b"PK-virtual")
    nfc_src = src_dir / unicodedata.normalize("NFC", nfd)           # 앱은 NFC 경로로 다룬다
    raw = executor._disk_name(nfc_src)
    assert raw.name == nfd
    os.link(raw, dst_dir / raw.name)
    os.unlink(raw)
    assert os.listdir(dst_dir) == [nfd]


def test_planner_splits_same_name_inside_plan_into_parent_folder(tmp_path):
    from tongdory2.plan.planner import build_plan

    cur = tmp_path / "dest"
    (cur / "참고").mkdir(parents=True)
    (cur / "참고" / "원래있음.txt").write_text("x")
    src = tmp_path / "src"
    files = []
    for sub in ("0626", "0622"):
        f = src / "backup" / sub / "asn.1.txt"
        f.parent.mkdir(parents=True)
        f.write_text(sub)
        files.append(f)
    keep = src / "원래있음.txt"
    keep.write_text("y")
    entries = [{"entry_id": f"e{i}", "path": str(f), "name": f.name, "verdict": "JOIN", "node_path": "참고",
                "sensitivity": "L1"} for i, f in enumerate(files + [keep])]
    plan = build_plan(entries, {"e0", "e1", "e2"}, [], cur, tmp_path / "store", tmp_path, False)
    dsts = sorted(Path(o.dst).relative_to(cur).as_posix() for o in plan.ops if o.op.value == "MOVE")
    assert dsts == ["참고/0622/asn.1.txt", "참고/asn.1.txt"] or dsts == ["참고/0626/asn.1.txt", "참고/asn.1.txt"]
    assert [s["name"] for s in plan.skipped] == ["원래있음.txt"]  # 목적지에 원래 있던 이름은 그대로 건너뜀


@pytest.mark.one_batch
def test_duplicate_goes_to_trash_even_if_trash_folder_missing(tmp_path):
    """#73 홈에 .Trash가 없어도 중복 후보를 휴지통으로 보낼 수 있다."""
    home = tmp_path.resolve() / "home"
    old = time.time() - 86400
    for n in ("a", "b"):
        f = home / "Desktop" / n / "회의록_1.txt"
        f.parent.mkdir(parents=True)
        f.write_text("같은 내용")
        os.utime(f, (old, old))
    (home / "Documents" / "정리" / "보고" / "회의록").mkdir(parents=True)
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(home.parent / "app"), LINER_API_KEY=""))
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.post("/setup/structure", data={"paths": "보고/회의록"})
    c.post("/setup/sources", data={"paths": "~/Desktop"})
    c.post("/setup/destination", data={"path": "~/Documents/정리"})
    c.get("/s2?new=1")
    for _ in range(200):
        page = c.get("/s2").text
        if 'name="prep_token"' in page:
            break
        time.sleep(0.03)
    c.post("/s2/analyze", data={"prep_token": re.search(r'name="prep_token" value="([^"]+)"', page).group(1)})
    store = CatalogStore(app.state.t.db.conn)
    for _ in range(200):
        if store.latest() and store.latest()["status"] == "DONE":
            break
        time.sleep(0.03)
    run_id = store.latest()["run_id"]
    dup = next(e for e in store.entries(run_id) if e["verdict"] == "DUP")
    c.post("/s2/check", data={"run_id": run_id, "entry_id": dup["entry_id"], "checked": "1"})
    assert not (home / ".Trash").exists()
    c.post(f"/s3/execute/{_plan(c, run_id)}", data={"confirm": "yes", "extra_confirm": "yes"})
    assert (home / ".Trash" / "회의록_1.txt").exists() and not Path(dup["path"]).exists()


def test_new_file_joins_previously_made_folder_group(tmp_path):
    """#75 묶음 폴더를 만든 뒤 같은 원래 폴더에 새 파일 하나가 생기면 기타가 아니라 그 묶음으로."""
    conn = sqlite3.connect(":memory:")
    _run([_pair(f"성능/03_시험/r{i}.md") for i in range(5)], tmp_path, conn)
    entries, _ = _run([_pair("성능/03_시험/새파일.md")], tmp_path, conn)
    assert entries[0].verdict == "GROUP" and entries[0].node_path == "자동분류/성능/03_시험"
