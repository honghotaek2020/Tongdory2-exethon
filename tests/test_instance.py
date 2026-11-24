"""이전에 실행한 통돌이만 정상 종료시키는지 검증한다."""

import json
import socket

from fastapi.testclient import TestClient

from tongdory2 import instance
from tongdory2.config import Settings
from tongdory2.web.app import create_app


class _Server:
    should_exit = False


def _app(tmp_path, token="secret"):
    s = Settings(_env_file=None, LINER_API_KEY="", TONGDORY_HOME=str(tmp_path), TONGDORY_DATA_DIR=str(tmp_path / "d"))
    app = create_app(s)
    instance.install_shutdown_route(app, token)
    app.state.server = _Server()
    return app, TestClient(app, base_url="http://127.0.0.1:8765")


def test_shutdown_requires_token_and_refuses_while_busy(tmp_path):
    app, client = _app(tmp_path)
    assert client.post(instance.SHUTDOWN_PATH).status_code == 403
    assert client.post(instance.SHUTDOWN_PATH, headers={instance.TOKEN_HEADER: "wrong"}).status_code == 403
    assert not app.state.server.should_exit
    app.state.t.job = {"running": True}
    assert client.post(instance.SHUTDOWN_PATH, headers={instance.TOKEN_HEADER: "secret"}).status_code == 409
    assert not app.state.server.should_exit
    app.state.t.job = {"running": False}
    assert client.post(instance.SHUTDOWN_PATH, headers={instance.TOKEN_HEADER: "secret"}).status_code == 202
    assert app.state.server.should_exit


def test_record_is_owned_and_unknown_holder_is_left_alone(tmp_path):
    data = tmp_path / "d"
    instance.write_record(data, 8765, "mine")
    instance.clear_record(data, "other")
    assert json.loads(instance.instance_file(data).read_text())["token"] == "mine"
    instance.clear_record(data, "mine")
    assert not instance.instance_file(data).exists()
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        assert not instance.port_free("127.0.0.1", port)
        # 실행 기록이 없거나 포트가 다르면 아무것도 끄지 않는다.
        assert instance.stop_previous(data, "127.0.0.1", port, wait=0.1) == "unknown"
        instance.write_record(data, port + 1, "mine")
        assert instance.stop_previous(data, "127.0.0.1", port, wait=0.1) == "unknown"


def test_db_close_all_closes_connections_from_other_threads(tmp_path):
    """Windows에서 임시 DB 폴더를 지우려면 요청 스레드들이 연 연결까지 모두 닫아야 한다."""
    import sqlite3
    import threading

    import pytest

    from tongdory2.db import DB

    db = DB(tmp_path / "t.db")
    seen = []
    worker = threading.Thread(target=lambda: seen.append(db.conn))
    worker.start()
    worker.join()
    main_conn = db.conn
    db.close_all()
    for conn in (seen[0], main_conn):
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    assert db.conn.execute("SELECT 1").fetchone()[0] == 1  # 닫은 뒤에도 새 연결로 다시 쓸 수 있다
