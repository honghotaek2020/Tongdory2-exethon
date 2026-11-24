"""화면에서 Liner API 키를 입력·교체·삭제하고, 앱 종료 버튼이 안전하게 동작하는지 검증한다."""

import os
import stat

import httpx
import pytest
import respx

from fastapi.testclient import TestClient

from tongdory2 import config
from tongdory2.config import Settings
from tongdory2.liner import keystore
from tongdory2.web.app import create_app
from tongdory2.web.routes import app_settings

KEY = "lnr-test-key-ABCD1234"
REAL_VERIFY = app_settings.verify_key


@pytest.fixture(autouse=True)
def _no_network_key_check(monkeypatch):
    """테스트에서는 Liner에 실제로 묻지 않는다. 필요한 테스트만 결과를 바꾼다."""
    async def ok(key, base_url):
        return "ok", 4.5
    monkeypatch.setattr(app_settings, "verify_key", ok)


def _client(tmp_path, monkeypatch):
    monkeypatch.delenv("LINER_API_KEY", raising=False)
    s = Settings(_env_file=None, LINER_API_KEY="", TONGDORY_HOME=str(tmp_path), TONGDORY_DATA_DIR=str(tmp_path / "data"))
    app = create_app(s)
    return app, TestClient(app, base_url="http://127.0.0.1:8765")


def test_save_applies_immediately_masks_and_restricts_file(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    assert "설정 안 됨" in client.get("/settings/liner").text
    assert "API 키 없음" in client.get("/").text
    r = client.post("/settings/liner", data={"action": "save", "key": f"  {KEY}  "}, follow_redirects=False)
    assert r.status_code == 303
    assert app.state.t.settings.liner_api_key == KEY  # 다시 시작하지 않아도 적용
    page = client.get("/settings/liner").text
    assert "…1234" in page and KEY not in page and "화면에서 저장한 키" in page
    assert "API 키 설정됨" in client.get("/").text
    saved = keystore.key_file(tmp_path / "data")
    assert saved.read_text() == f"{KEY}\n" and not (tmp_path / "data" / ".env").exists()  # .env에는 쓰지 않는다
    if os.name != "nt":
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_invalid_key_is_rejected_and_delete_removes_key_file(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    saved = keystore.key_file(tmp_path / "data")
    client.post("/settings/liner", data={"action": "save", "key": "old-key-0000"})
    for bad in ("", "short", "has space inside key", "line\nbreak-123456"):
        client.post("/settings/liner", data={"action": "save", "key": bad})
    assert saved.read_text() == "old-key-0000\n"
    client.post("/settings/liner", data={"action": "save", "key": KEY})
    assert saved.read_text() == f"{KEY}\n"
    client.post("/settings/liner", data={"action": "delete"})
    assert not saved.exists() and app.state.t.settings.liner_api_key == ""


def test_priority_env_then_screen_and_env_file_key_is_ignored(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / ".env").write_text("LINER_API_KEY=old-env-file-9999\n")  # 예전 방식 파일은 무시
    page = client.get("/settings/liner").text
    assert "설정 안 됨" in page
    client.post("/settings/liner", data={"action": "save", "key": KEY})
    assert app.state.t.settings.liner_api_key == KEY
    client.post("/settings/liner", data={"action": "delete"})
    assert app.state.t.settings.liner_api_key == ""  # 삭제하면 .env 파일 키로 돌아가지 않는다
    monkeypatch.setenv("LINER_API_KEY", "env-key-7777")
    client.post("/settings/liner", data={"action": "save", "key": KEY})
    assert app.state.t.settings.liner_api_key == "env-key-7777"  # 환경 변수가 가장 우선
    assert "환경 변수" in client.get("/settings/liner").text


def test_get_settings_ignores_repo_env_key_but_keeps_other_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("LINER_API_KEY", raising=False)
    monkeypatch.delenv("TONGDORY_PORT", raising=False)
    repo_env = tmp_path / "repo.env"
    repo_env.write_text("LINER_API_KEY=repo-key-9999\nTONGDORY_PORT=8799\n")
    monkeypatch.setattr(config, "env_file_path", lambda compiled=None: repo_env)
    monkeypatch.setenv("TONGDORY_DATA_DIR", str(tmp_path / "data"))
    config.reset_settings_cache()
    s = config.get_settings()
    assert s.liner_api_key == "" and s.port == 8799
    keystore.write(keystore.key_file(tmp_path / "data"), KEY)
    config.reset_settings_cache()
    assert config.get_settings().liner_api_key == KEY
    config.reset_settings_cache()


def test_cross_site_key_change_is_blocked(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    r = client.post("/settings/liner", data={"action": "save", "key": KEY}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and app.state.t.settings.liner_api_key == ""


def test_quit_button_stops_server_unless_busy(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    assert "앱 종료" in client.get("/").text

    class Server:
        should_exit = False

    app.state.server = Server()
    app.state.t.job = {"running": True}
    client.post("/app/quit")
    assert not app.state.server.should_exit
    app.state.t.job = {"running": False}
    r = client.post("/app/quit")
    assert r.status_code == 200 and "종료했습니다" in r.text and app.state.server.should_exit


def test_return_to_only_allows_app_paths():
    ok = "/s2?batch=1&tab=check&page=1&result=1"
    assert app_settings.safe_return(ok) == ok
    for bad in ("https://evil.example/", "//evil.example/x", "/\\evil", "javascript:alert(1)", "s2", "/settings/liner?x=1", ""):
        assert app_settings.safe_return(bad) == ""


def test_rejected_key_is_not_saved_and_unreachable_key_is_saved_with_notice(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)

    async def rejected(key, base_url):
        return "invalid", None
    monkeypatch.setattr(app_settings, "verify_key", rejected)
    page = client.post("/settings/liner", data={"action": "save", "key": KEY}).text
    assert app.state.t.settings.liner_api_key == "" and not keystore.key_file(tmp_path / "data").exists()
    assert "거부했습니다" in page

    async def offline(key, base_url):
        return "unreachable", None
    monkeypatch.setattr(app_settings, "verify_key", offline)
    page = client.post("/settings/liner", data={"action": "save", "key": KEY}).text
    assert app.state.t.settings.liner_api_key == KEY
    assert "확인하지는 못했습니다" in page


def test_saved_message_shows_checked_balance(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    page = client.post("/settings/liner", data={"action": "save", "key": KEY}).text
    assert "잔액 $4.50" in page


@respx.mock
async def test_verify_key_uses_free_balance_call():
    base = "https://platform.example/api/v1"
    route = respx.get(f"{base}/credits/balance").mock(
        side_effect=[httpx.Response(200, json={"balance_usd": 12.5}),
                     httpx.Response(401, json={"error": {"code": "invalid_api_key", "message": "bad"}})])
    assert await REAL_VERIFY(KEY, base) == ("ok", 12.5)
    assert await REAL_VERIFY(KEY, base) == ("invalid", None)
    assert route.calls[0].request.headers.get("authorization", "").endswith(KEY)
