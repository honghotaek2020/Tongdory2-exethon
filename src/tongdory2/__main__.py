"""`uv run tongdory` 진입점: 로컬 서버(127.0.0.1) 기동 + 브라우저 열기."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import secrets
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from tongdory2.config import get_settings
from tongdory2.instance import clear_record, install_shutdown_route, port_free, stop_previous, write_record
from tongdory2.branding import APP_NAME, APP_TAGLINE
from tongdory2.liner import keystore
from tongdory2.web.app import create_app


def _windowed() -> bool:
    """창 없이 실행 중인가 (macOS .app 번들, 콘솔 없는 Windows exe)."""
    return sys.stdout is None or sys.stderr is None or ".app/Contents/MacOS" in sys.executable


def _alert(message: str) -> None:
    """창 없이 실행했을 때 사용자에게 오류를 알린다 (터미널이 없으므로 로그만으로는 보이지 않는다)."""
    if not WINDOWED or CHECK_MODE:  # 점검 모드에서는 창을 띄우지 않는다 (빌드가 확인 클릭을 기다리며 멈춤)
        return
    try:
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
        elif sys.platform == "darwin":
            import subprocess

            script = f"display alert {json.dumps(APP_NAME)} message {json.dumps(message)} as critical"
            subprocess.run(["osascript", "-e", script], check=False, timeout=120)
    except Exception:  # 알림 실패는 로그로 충분하다
        pass


def _log_to_file(data_dir: Path) -> Path | None:
    """앱 데이터 폴더 logs/에 실행 로그를 남긴다 (창 없는 배포본에서 문제를 확인하는 곳)."""
    try:
        logs = data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(logs / "tongdory.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    for name in ("", "uvicorn", "uvicorn.error"):
        logging.getLogger(name).addHandler(handler)
    return logs / "tongdory.log"


WINDOWED = _windowed()
CHECK_MODE = "--check-installation" in sys.argv
DETACHED_ENV = "TONGDORY_DETACHED"


def _detach_from_app_bundle(args: argparse.Namespace) -> bool:
    """macOS .app으로 실행했으면 서버를 별도 프로세스로 띄우고 이 프로세스는 바로 끝낸다.

    macOS는 실행 중인 .app을 다시 더블클릭해도 새로 실행하지 않는다. 앱 프로세스가 곧바로 끝나면
    다시 더블클릭할 때마다 새로 실행되고, 이전 통돌이를 정리한 뒤 브라우저를 다시 열 수 있다.
    """
    if (sys.platform != "darwin" or ".app/Contents/MacOS" not in sys.executable
            or os.environ.get(DETACHED_ENV) or args.check_installation):
        return False
    import subprocess

    env = dict(os.environ, **{DETACHED_ENV: "1"})
    subprocess.Popen([sys.executable, *sys.argv[1:]], env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    return True


def main() -> None:
    try:
        _main()
    except SystemExit:
        raise
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            return
        logging.getLogger(__name__).exception("[오류] 파일 통돌이를 실행하지 못했습니다.")
        _alert(f"파일 통돌이를 실행하지 못했습니다: {exc}\n자세한 내용은 앱 데이터 폴더의 logs/tongdory.log를 확인하세요.")
        sys.exit(1)


def _main() -> None:
    if sys.stdout is None or sys.stderr is None:
        # 콘솔 없는 Windows exe는 표준 출력이 없다. 출력하는 코드가 멈추지 않도록 버린다.
        devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stdout or devnull
        sys.stderr = sys.stderr or devnull
    ap = argparse.ArgumentParser(prog="tongdory", description=f"{APP_NAME} — {APP_TAGLINE}")
    ap.add_argument("--os", choices=("auto", "macos", "windows"), default="auto",
                    help="실행할 운영체제 확인 (기본값: 자동 감지)")
    ap.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    ap.add_argument("--check-installation", action="store_true", help="임시 데이터로 실행본을 점검하고 종료")
    args = ap.parse_args()
    current_os = {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    if args.os not in ("auto", current_os):
        ap.error(f"현재 운영체제는 {current_os}입니다. --os {args.os}로 실행할 수 없습니다.")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger(__name__)
    log.info("[실행 환경] 운영체제=%s", current_os)
    if _detach_from_app_bundle(args):
        return
    if args.check_installation:
        from tongdory2.install_check import check_installation

        check_installation()
        return
    s = get_settings()
    log_file = _log_to_file(s.data_dir)
    key = keystore.status(keystore.key_file(s.data_dir))
    log.info("[설정] Liner 키=%s%s, 로그=%s", "설정됨" if s.liner_api_key else "미설정",
             f" ({key.label})" if key.key else " (화면의 [Liner API 키]에서 입력)", log_file or "저장 안 함")
    if not port_free(s.host, s.port):
        log.info("[시작] %s 포트를 이미 사용 중입니다. 이전에 실행한 파일 통돌이인지 확인합니다.", s.port)
        result = stop_previous(s.data_dir, s.host, s.port)
        if result == "stopped":
            log.info("[시작] 이전에 실행 중이던 파일 통돌이를 종료했습니다.")
        else:
            if result == "busy":
                # 작업 중인 통돌이는 끄지 않고 그 화면을 다시 연다.
                log.info("[시작] 이전에 실행한 파일 통돌이가 작업 중이라 그 화면을 엽니다.")
                if not args.no_browser:
                    webbrowser.open(s.app_url)
                return
            else:
                message = (f"{s.port} 포트를 다른 프로그램이 쓰고 있습니다. 그 프로그램을 종료하거나, "
                           f"아니면 TONGDORY_PORT={s.port + 1} 처럼 다른 포트로 실행하세요.")
            log.error("[오류] %s", message)
            _alert(message)
            sys.exit(1)

    token = secrets.token_hex(16)
    app = create_app(s)
    install_shutdown_route(app, token)
    server = uvicorn.Server(uvicorn.Config(app, host=s.host, port=s.port, log_level="warning"))
    app.state.server = server
    # Ctrl+C·종료 신호로 끝날 때도 서버 종료 단계에서 실행 기록을 지운다.
    app.router.on_shutdown.append(lambda: clear_record(s.data_dir, token))
    url = s.app_url

    def open_browser_when_ready() -> None:
        deadline = time.monotonic() + 15
        while not server.started and not server.should_exit and time.monotonic() < deadline:
            time.sleep(0.1)
        if server.started:
            webbrowser.open(url)

    if not args.no_browser:
        threading.Thread(target=open_browser_when_ready, daemon=True).start()
    log.info("[시작] %s", url)
    if s.fallback_url != url:
        log.info("[시작] 주소가 열리지 않으면 %s 로 접속하세요.", s.fallback_url)
    write_record(s.data_dir, s.port, token)
    try:
        server.run()
    finally:
        clear_record(s.data_dir, token)


if __name__ == "__main__":
    main()
