"""묶음 재개, 브라우저 간 충돌, 인수인계 파일 생성 검증."""

import json
import os
import re
import threading
import time
from html import unescape

import httpx
from fastapi.testclient import TestClient

from tongdory2.catalog import inventory
from tongdory2.catalog.pipeline import MAX_ITEMS
from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.fsx.roots import RegisteredRoot, Role
from tongdory2.fsx.scope import Scope
from tongdory2.liner import client as liner
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import create_app
from tongdory2.web.routes import s4
from tongdory2.web.setup_state import scope_key


def wait_for(client, path, phrase):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        response = client.get(path)
        if phrase in response.text:
            return response
        time.sleep(.025)
    raise AssertionError(response.text[:800])


def token(response, name):
    return re.search(f'name="{name}" value="([^"]+)"', response.text).group(1)


def test_full_batch_selection_fits_both_action_forms(tmp_path):
    home = tmp_path.resolve() / "home"
    source = home / "Desktop"
    destination = home / "Documents" / "정리"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    original = source / "가상회의록.txt"
    original.write_text("가상 회의록")
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(home.parent / "app"), LINER_API_KEY=""))
    client = TestClient(app, base_url="http://127.0.0.1:8765", follow_redirects=False)
    client.post("/setup/structure", data={"paths": "보고/회의록"})
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    client.post("/setup/destination", data={"path": "~/Documents/정리"})
    t = app.state.t
    spec = SpecStore(t.db.conn).current()
    node = next(n["node_id"] for n in spec["body"]["nodes"] if n["display_path"] == "보고/회의록")
    catalog = CatalogStore(t.db.conn)
    rid = catalog.start(spec["version"], {"scope_key": scope_key(t.db.list_roots(), spec)})
    catalog.finish(rid, [{"entry_id": "e0000", "path": str(original), "name": original.name,
                          "verdict": "SPEC_NODE", "node_path": "보고/회의록", "sensitivity": "L1"}], 0)
    selected = [f"e{i:04d}" for i in range(MAX_ITEMS)]

    assigned = client.post("/s2/assign", data={"run_id": rid, "target": node, "entry_ids": selected})
    assert assigned.status_code == 303 and assigned.headers["location"] == "/s2"  # #8 #shots는 스크린샷 폼에서만
    assert catalog.entries(rid)[0]["override"] == node
    planned = client.post("/s3/plan", data={"run_id": rid, "sel": selected})
    assert planned.status_code == 303 and planned.headers["location"].startswith("/s3/plan/")
    assert client.post("/s3/plan", data={"run_id": rid, "sel": selected + ["extra"]}).status_code in (400, 422)


def test_batches_resume_and_two_browsers_share_only_work(tmp_path, monkeypatch):
    scans = []
    scan = inventory.scan_collect

    def count_scan(*args, **kwargs):
        scans.append(1)
        return scan(*args, **kwargs)

    monkeypatch.setattr(inventory, "scan_collect", count_scan)
    from tongdory2.catalog import survey  # 전체 탐색은 이제 '파일 종류 살펴보기'에서 한 번 한다

    monkeypatch.setattr(survey, "scan_collect", count_scan)
    home = tmp_path.resolve() / "home"
    desktop = home / "Desktop"
    saved = home / "Documents" / "정리"
    desktop.mkdir(parents=True)
    saved.mkdir(parents=True)
    now = time.time()
    for number in range(5):
        file = desktop / f"가상파일-{number}.txt"
        file.write_text(f"가상 내용 {number}")
        os.utime(file, (now - number, now - number))  # 최근 수정으로 분류하고 외부에는 보내지 않는다.
    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(home.parent / "app"),
                        TONGDORY_BATCH_SIZE=2, LINER_API_KEY="")
    app = create_app(settings)
    base = "http://127.0.0.1:8765"
    a = TestClient(app, base_url=base)
    b = TestClient(app, base_url=base)
    rev_a = token(a.get("/setup/structure"), "revision")
    rev_b = token(b.get("/setup/structure"), "revision")
    assert rev_a == rev_b
    assert a.cookies["tongdory_browser"] != b.cookies["tongdory_browser"]
    a.post("/setup/structure", data={"paths": "보고/회의록", "revision": rev_a})
    b_conflict = b.post("/setup/structure", data={"paths": "잘못 덮을 구조", "revision": rev_b})
    assert b_conflict.status_code == 422 and "다른 화면에서 설정이 변경" in b_conflict.text
    a.post("/setup/sources", data={"paths": "~/Desktop"})
    a.post("/setup/destination", data={"path": "~/Documents/정리"})
    assert "설정 3/3" in b.get("/").text
    a.post("/s2/analyze", data={"prep_token": "bad"}, follow_redirects=False)
    assert "이미 처리됐습니다" not in b.get("/").text
    assert "이미 처리됐습니다" in a.get("/").text

    a.get("/s2?new=1", follow_redirects=False)
    initial_token = token(wait_for(a, "/s2", 'name="prep_token"'), "prep_token")
    app = create_app(settings)
    a = TestClient(app, base_url=base)
    b = TestClient(app, base_url=base)
    assert token(wait_for(a, "/s2", 'name="prep_token"'), "prep_token") != initial_token
    sequences = []
    for number, expected in enumerate((2, 2, 1), 1):
        ready = wait_for(a, "/s2", 'name="prep_token"')
        assert f"{number}번째 묶음" in ready.text
        approval = token(ready, "prep_token")
        assert token(b.get("/s2"), "prep_token") == approval
        a.post("/s2/analyze", data={"prep_token": approval}, follow_redirects=False)
        b.post("/s2/analyze", data={"prep_token": approval}, follow_redirects=False)
        wait_for(a, "/s2", 'name="run_id"')
        catalog = CatalogStore(app.state.t.db.conn)
        run = catalog.latest()
        assert run["status"] == "DONE"
        notice = json.loads(run["notice"])
        assert notice["batch_number"] == number and notice["scanned"] == expected
        assert notice["truncated"] == 5 - sum((2, 2, 1)[:number])
        sequences += [e["name"] for e in catalog.entries(run["run_id"])]
        if number == 1:  # 서버 재시작 뒤에도 같은 묶음 시퀀스를 이어갈 수 있다.
            app = create_app(settings)
            a = TestClient(app, base_url=base)
            b = TestClient(app, base_url=base)
        if number < 3:
            a.post("/s2/next", data={"run_id": run["run_id"]}, follow_redirects=False)
            b.post("/s2/next", data={"run_id": run["run_id"]}, follow_redirects=False)
    assert sequences == [f"가상파일-{i}.txt" for i in range(5)]
    assert 'action="/s2/next"' not in a.get("/s2").text  # 마지막 묶음 뒤에는 [다음 묶음 분류] 버튼이 없다
    with app.state.t.db.conn as conn:
        assert conn.execute("SELECT COUNT(*) FROM catalog_runs WHERE kind='COLLECT'").fetchone()[0] == 3
    assert len(scans) == 1


def test_inventory_change_requires_new_approval_and_refresh_finds_new_files(tmp_path):
    home = tmp_path.resolve() / "home"
    desktop = home / "Desktop"
    dest = home / "Documents" / "정리"
    desktop.mkdir(parents=True)
    dest.mkdir(parents=True)
    original = desktop / "원본.txt"
    old = desktop / "삭제할파일.txt"
    old.write_text("자료")
    original.write_text("AAAA")
    now = time.time()
    os.utime(old, (now - 60, now - 60))
    os.utime(original, (now, now))
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(home.parent / "app"),
                              TONGDORY_BATCH_SIZE=1, LINER_API_KEY=""))
    c = TestClient(app, base_url="http://127.0.0.1:8765", follow_redirects=False)
    c.post("/setup/structure", data={"paths": "보고"})
    c.post("/setup/sources", data={"paths": "~/Desktop"})
    c.post("/setup/destination", data={"path": "~/Documents/정리"})
    catalog = CatalogStore(app.state.t.db.conn)

    c.get("/s2?new=1")
    ready = wait_for(c, "/s2", 'name="prep_token"')
    previous_token = token(ready, "prep_token")
    st = original.stat()
    original.write_text("BBBB")
    os.utime(original, ns=(st.st_atime_ns, st.st_mtime_ns))
    c.post("/s2/analyze", data={"prep_token": previous_token})
    wait_for(c, "/s2", "파일이 바뀌었습니다")
    failed = catalog.latest()
    assert failed["status"] == "FAILED" and failed["error"] == "FILES_CHANGED"
    assert catalog.entries(failed["run_id"]) == []
    c.post("/s2/retry", data={"run_id": failed["run_id"]})
    ready = wait_for(c, "/s2", 'name="prep_token"')
    assert token(ready, "prep_token") != previous_token
    c.post("/s2/analyze", data={"prep_token": token(ready, "prep_token")})
    wait_for(c, "/s2", "파일 분류 결과 검토")
    first = catalog.latest()
    assert json.loads(failed["notice"])["series_id"] == json.loads(first["notice"])["series_id"]
    assert [e["name"] for e in catalog.entries(first["run_id"])] == [original.name]

    old.unlink()
    original.write_text("CCCC")
    os.utime(original, ns=(st.st_atime_ns, st.st_mtime_ns))
    added = desktop / "새파일.txt"
    added.write_text("새 자료")
    c.post("/s2/refresh", data={"run_id": first["run_id"]})
    ready = wait_for(c, "/s2", 'name="prep_token"')
    assert "건너뛴 항목 1개" in ready.text
    c.post("/s2/analyze", data={"prep_token": token(ready, "prep_token")})
    wait_for(c, "/s2", "파일 분류 결과 검토")
    final = catalog.latest()
    assert [e["name"] for e in catalog.entries(final["run_id"])] == [added.name]
    notice = json.loads(final["notice"])
    assert (notice["prior_analyzed"], notice["truncated"], notice["inventory_skipped"]) == (1, 1, 1)
    c.post("/s2/next", data={"run_id": final["run_id"]})
    ready = wait_for(c, "/s2", 'name="prep_token"')
    c.post("/s2/analyze", data={"prep_token": token(ready, "prep_token")})
    wait_for(c, "/s2", "파일 분류 결과 검토")
    latest = catalog.latest()
    assert [e["name"] for e in catalog.entries(latest["run_id"])] == [original.name]
    assert json.loads(latest["notice"])["truncated"] == 0


def test_inventory_skips_files_that_enter_a_protected_folder(tmp_path):
    home = tmp_path.resolve() / "home"
    source = home / "Desktop"
    repository = source / "문서"
    repository.mkdir(parents=True)
    (repository / "자료.txt").write_text("가상 자료")
    outside = tmp_path.resolve() / "외부자료.txt"
    outside.write_text("범위 밖 자료")
    link = source / "이동할자료.txt"
    link.write_text("가상 자료")
    root = RegisteredRoot("src", source, Role.COLLECT, True)
    scope = Scope([root], home, tmp_path / "data")
    db = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"))).state.t.db
    stored = inventory.InventoryStore(db.conn)
    stored.rebuild("series", "scope", [root], scope, False, progress=None)

    (repository / ".git").mkdir()
    link.unlink()
    link.symlink_to(outside)
    batch = stored.select("series", scope, 2)
    assert batch.items == [] and batch.skipped == 2


def test_handover_background_job_is_shared_and_stale_submit_is_rejected(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "home"
    desktop = home / "Desktop"
    saved = home / "Documents" / "저장"
    dest = saved / "보고" / "회의록"
    desktop.mkdir(parents=True)
    dest.mkdir(parents=True)
    (dest / "가상회의록.txt").write_text("로컬 가상 파일")
    (saved / "README.md").write_text("기존 README")
    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(home.parent / "app"), LINER_API_KEY="")
    app = create_app(settings)
    a = TestClient(app, base_url="http://127.0.0.1:8765")
    b = TestClient(app, base_url="http://127.0.0.1:8765")
    a.post("/setup/structure", data={"paths": "보고/회의록", "required": "보고/회의록"})
    a.post("/setup/sources", data={"paths": "~/Desktop"})
    a.post("/setup/destination", data={"path": "~/Documents/저장"})
    stamp = token(a.get("/s4"), "stamp")
    assert token(b.get("/s4"), "stamp") == stamp

    start, release = threading.Event(), threading.Event()
    build = s4.build_checklist

    def slow(*args):
        start.set()
        assert release.wait(4)
        return build(*args)

    monkeypatch.setattr(s4, "build_checklist", slow)
    try:
        a.post("/s4/readme", data={"stamp": stamp}, follow_redirects=False)
        assert start.wait(3)
        assert "인수인계 초안 작성 중" in b.get("/s4").text
        b.post("/s4/readme", data={"stamp": stamp}, follow_redirects=False)
        assert a.get("/").status_code == 200
    finally:
        release.set()

    page = wait_for(a, "/s4", "README_인수인계.md 열기")
    assert token(page, "stamp") != stamp
    b.post("/s4/readme", data={"stamp": stamp}, follow_redirects=False)
    assert "인수인계 정보가 갱신" in b.get("/s4").text
    assert len(list(saved.glob("README_인수인계*.md"))) == 1
    assert (saved / "README.md").read_text() == "기존 README"
    response = a.get("/s4/draft/README_인수인계.md")
    assert "다음 담당자 확인 사항" in response.text and "가상회의록.txt" in response.text
    assert response.headers["content-disposition"].startswith("inline")
    assert a.get("/s4/draft/README_인수인계.md?download=1").headers["content-disposition"].startswith("attachment")
    assert a.get("/s4/draft/README.md").status_code == 404


def test_handover_sends_only_the_previewed_safe_payload(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "home"
    source = home / "Desktop"
    saved = home / "Documents" / "저장"
    folder = saved / "회의록"
    source.mkdir(parents=True)
    folder.mkdir(parents=True)
    safe = folder / "공유 회의록.txt"
    changing = folder / "변경될 자료.txt"
    classified = folder / "이력상 보호 자료.txt"
    safe.write_text("본문은 보내지 않습니다.")
    changing.write_text("가상 초기 파일")
    classified.write_text("이름과 지금 내용으로는 보류 사유를 알 수 없음")

    sent = []
    api_type = liner.LinerClient

    def respond(request):
        body = json.loads(request.content)
        sent.append(body["messages"][1]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content":
            json.dumps({"summary": "테스트 요약", "next_owner_tips": []})}}]})

    monkeypatch.setattr(liner, "LinerClient",
                        lambda *args, **kwargs: api_type(*args, transport=httpx.MockTransport(respond), **kwargs))
    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "app"), LINER_API_KEY="fake-handover-key")
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765", follow_redirects=False)
    client.post("/setup/structure", data={"paths": "회의록", "required": "회의록"})
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    client.post("/setup/destination", data={"path": "~/Documents/저장"})
    t = app.state.t
    spec = SpecStore(t.db.conn).current()
    store = CatalogStore(t.db.conn)
    run_id = store.start(spec["version"], {"scope_key": scope_key(t.db.list_roots(), spec)})
    store.finish(run_id, [{"entry_id": "e0", "name": classified.name, "path": str(classified),
                           "verdict": "EXCLUDE", "sensitivity": "L3"}], 0)

    preview = client.get("/s4?preview_liner=1")
    payload = unescape(re.search(r"<pre[^>]*>(.*?)</pre>", preview.text, re.S)[1])
    assert safe.name in payload and changing.name in payload and classified.name not in payload
    assert "본문은 보내지 않습니다" not in payload
    fields = {"stamp": token(preview, "stamp"), "use_liner": "true"}
    assert client.post("/s4/readme", data=fields).headers["location"] == "/s4?preview_liner=1#liner-preview"
    assert not sent

    changing.write_text("DEMO_SECRET=가상 비밀값")
    client.post("/s4/readme", data=fields | {"approval": token(preview, "approval")})
    wait_for(client, "/s4", "전송할 내용이 바뀌었거나")
    assert not sent and not list(saved.glob("README_인수인계*.md"))

    preview = client.get("/s4?preview_liner=1")
    payload = unescape(re.search(r"<pre[^>]*>(.*?)</pre>", preview.text, re.S)[1])
    assert safe.name in payload and changing.name not in payload and classified.name not in payload
    client.post("/s4/readme", data=fields | {"approval": token(preview, "approval")})
    wait_for(client, "/s4", "README_인수인계.md 열기")
    assert sent == [payload]
    assert "테스트 요약" in (saved / "README_인수인계.md").read_text()
