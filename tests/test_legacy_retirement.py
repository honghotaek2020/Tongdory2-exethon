"""이전 설정 화면 제거와 협업 대상 등록의 현재 화면 이관을 검증한다."""

import re
import time

import pytest
from fastapi.testclient import TestClient

from tongdory2.config import Settings
from tongdory2.fsx.roots import Role
from tongdory2.web.app import create_app


@pytest.fixture
def env(tmp_path):
    home = tmp_path.resolve() / "home"
    for name in ("Desktop", "Documents/정리", "Documents/협업", "Documents/다른협업", "Dropbox/협업"):
        (home / name).mkdir(parents=True)
    (home / "Documents/협업/설계.md").write_text("# 설계\n협업 문서", encoding="utf-8")
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY=""))
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    return client, app.state.t, home


def setup(client):
    assert client.post("/setup/structure", data={"paths": "보고/회의록"}, follow_redirects=False).status_code == 303
    assert client.post("/setup/sources", data={"paths": "~/Desktop"}, follow_redirects=False).status_code == 303
    assert client.post("/setup/destination", data={"path": "~/Documents/정리"}, follow_redirects=False).status_code == 303


@pytest.mark.parametrize(("old", "current"), [("/s0", "/setup/sources"), ("/s1", "/setup/structure")])
def test_old_bookmarks_open_current_setup(env, old, current):
    client, _, _ = env
    response = client.get(old, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == current
    assert client.get(old).status_code == 200


@pytest.mark.parametrize("path", ["/s0/add", "/s0/add_many", "/s0/confirm", "/s0/remove/any", "/s0/link/any", "/s1/confirm"])
def test_old_mutation_apis_are_removed(env, path):
    client, t, _ = env
    assert client.post(path).status_code == 404
    assert t.db.list_roots() == []


def test_collaboration_redirects_to_pending_current_setup(env):
    client, _, _ = env
    assert client.get("/s2a", follow_redirects=False).headers["location"] == "/setup/structure"
    client.post("/setup/structure", data={"paths": "보고"})
    assert client.get("/s2a", follow_redirects=False).headers["location"] == "/setup/sources"
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    assert client.get("/s2a", follow_redirects=False).headers["location"] == "/setup/destination"


def test_targets_register_analyze_and_unregister_without_changing_setup(env):
    client, t, home = env
    setup(client)
    before = t.db.list_roots()
    current = next(r for r in before if r.role == Role.CURRENT)
    assert 'action="/s2a/targets"' in client.get("/s2a").text
    assert client.post("/s2a/targets", data={"paths": "~/Documents/협업", "linked": current.root_id},
                       follow_redirects=False).status_code == 303
    root = next(r for r in t.db.list_roots() if r.role == Role.IN_PLACE)
    assert root.confirmed and root.linked_root_id == current.root_id
    # 동일 대상을 다시 저장해도 ID와 연결은 유지한다.
    client.post("/s2a/targets", data={"paths": "~/Documents/협업"})
    assert next(r for r in t.db.list_roots() if r.role == Role.IN_PLACE) == root
    client.post("/s2a/analyze")
    deadline = time.monotonic() + 5
    while t.ip_job["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not t.ip_job["running"]
    assert "설계.md" in client.get("/s2a").text
    # 색인을 기존 연결 위치에 생성하고, 등록 해제는 실제 파일을 보존한다.
    assert client.post("/s2a/index", follow_redirects=False).status_code == 303
    assert (current.path / "README_문서색인.md").is_file()
    client.post("/s2a/targets", data={"paths": ""})
    assert t.db.list_roots() == before
    assert (home / "Documents/협업/설계.md").is_file()
    for name in ("01.보류함", "02.지식센터", "03.내보내기"):
        assert (home / "Documents/자료보관센터" / name).is_dir()


def test_target_validation_is_atomic_and_requires_warning_ack(env):
    client, t, _ = env
    setup(client)
    before = t.db.list_roots()
    response = client.post("/s2a/targets", data={"paths": "~/Documents/협업\n/System/Library"})
    assert "시스템 폴더는 등록할 수 없습니다" in response.text
    assert t.db.list_roots() == before
    response = client.post("/s2a/targets", data={"paths": "~/Dropbox/협업"})
    assert 'name="confirm_warnings"' in response.text
    assert t.db.list_roots() == before
    client.post("/s2a/targets", data={"paths": "~/Dropbox/협업", "confirm_warnings": "true"})
    assert any(r.role == Role.IN_PLACE and r.confirmed and r.acks for r in t.db.list_roots())


def test_targets_reject_busy_stale_or_invalid_link_and_clear_approval(env):
    client, t, _ = env
    setup(client)
    revision = re.search(r'name="revision" value="([^"]+)"', client.get("/s2a").text).group(1)
    before = t.db.list_roots()
    t.ip_job = {"running": True}
    client.post("/s2a/targets", data={"paths": "~/Documents/협업"}, follow_redirects=False)
    assert t.db.list_roots() == before
    t.ip_job = {"running": False}
    client.post("/s2a/targets", data={"paths": "~/Documents/협업", "linked": "invalid"})
    assert t.db.list_roots() == before
    t.ip_pending = {"token": "old-preview"}
    client.post("/s2a/targets", data={"paths": "~/Documents/협업", "revision": revision})
    assert t.ip_pending is None
    saved = t.db.list_roots()
    response = client.post("/s2a/targets", data={"paths": "~/Documents/다른협업", "revision": revision})
    assert "설정이 바뀌었습니다" in response.text
    assert t.db.list_roots() == saved


def test_target_form_escapes_paths_and_blocks_cross_origin(env):
    client, t, home = env
    setup(client)
    folder = home / "Documents/<img src=x onerror=alert(1)>"
    folder.mkdir()
    response = client.post("/s2a/targets", data={"paths": str(folder)})
    assert "<img src=x onerror" not in response.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in response.text
    before = t.db.list_roots()
    assert client.post("/s2a/targets", data={"paths": ""}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert t.db.list_roots() == before
