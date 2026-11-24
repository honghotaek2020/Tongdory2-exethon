"""이미 실행 중인 통돌이를 찾아 정상 종료시키고 같은 포트로 다시 시작한다.

다른 프로그램을 잘못 끄지 않도록, 앱 데이터 폴더의 실행 기록(instance.json)에 남긴
비밀 토큰을 아는 경우에만 이전 통돌이에게 종료를 요청한다. 강제 종료(kill)는 하지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)
SHUTDOWN_PATH = "/__tongdory/shutdown"
TOKEN_HEADER = "X-Tongdory-Token"  # 사용자 지정 헤더라 다른 웹사이트는 사전 확인 없이 보낼 수 없다


def instance_file(data_dir: Path) -> Path:
    return data_dir / "instance.json"


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name != "nt":  # uvicorn과 같은 조건으로 확인한다 (Windows에서는 포트 공유가 되므로 쓰지 않음)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def busy_jobs(app: FastAPI) -> bool:
    """분석·정리 같은 작업이 진행 중인가 (진행 중이면 종료 요청을 거절한다)."""
    t = app.state.t
    return any(isinstance(v, dict) and v.get("running") for v in vars(t).values())


def install_shutdown_route(app: FastAPI, token: str) -> None:
    @app.post(SHUTDOWN_PATH, include_in_schema=False)
    def shutdown(request: Request):
        if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
            return JSONResponse({"ok": False}, status_code=403)
        if busy_jobs(request.app):
            return JSONResponse({"ok": False, "reason": "busy"}, status_code=409)
        server = getattr(request.app.state, "server", None)
        if server is None:
            return JSONResponse({"ok": False}, status_code=503)
        log.info("[종료] 새로 실행한 통돌이의 요청으로 종료합니다.")
        server.should_exit = True  # 처리 중인 요청을 마친 뒤 정상 종료
        return JSONResponse({"ok": True}, status_code=202)


def write_record(data_dir: Path, port: int, token: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = instance_file(data_dir)
    path.write_text(json.dumps({"pid": os.getpid(), "port": port, "token": token}), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def clear_record(data_dir: Path, token: str) -> None:
    """내가 남긴 기록일 때만 지운다 (그사이 새로 시작한 통돌이의 기록은 남긴다)."""
    path = instance_file(data_dir)
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("token") == token:
            path.unlink()
    except (OSError, ValueError):
        pass


def stop_previous(data_dir: Path, host: str, port: int, wait: float = 15.0) -> str:
    """이전 통돌이에게 종료를 요청한다. 결과: stopped | busy | unknown."""
    try:
        record = json.loads(instance_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    if record.get("port") != port or not record.get("token"):
        return "unknown"
    request = urllib.request.Request(f"http://{host}:{port}{SHUTDOWN_PATH}", method="POST",
                                     headers={TOKEN_HEADER: record["token"]})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 202:
                return "unknown"
    except urllib.error.HTTPError as exc:
        return "busy" if exc.code == 409 else "unknown"
    except OSError:
        return "unknown"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if port_free(host, port):
            return "stopped"
        time.sleep(0.2)
    return "unknown"
