"""FastAPI 앱 팩토리. 서버 렌더링(Jinja2 autoescape) — 클라이언트 JS 빌드 없음."""

from __future__ import annotations

from collections import OrderedDict
from contextvars import ContextVar
from pathlib import Path
import re
import secrets
from threading import RLock
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from tongdory2.config import Settings, get_settings
from tongdory2.branding import APP_NAME, APP_TAGLINE, FOLDER_LIST_STEM
from tongdory2.db import DB
from tongdory2.fsx.paths import real
from tongdory2.fsx.scope import Scope
from tongdory2.messages import badge_label, error_text, folder_reason, js_messages, msg, reason_brief, stage_label

BROWSER_COOKIE = "tongdory_browser"
_browser: ContextVar[str | None] = ContextVar("tongdory_browser", default=None)

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))  # autoescape 기본 활성


def _progress(request):
    from tongdory2.web.progress import state

    return state(request.app.state.t)


def _localtime(v):
    from tongdory2.web.progress import local_time

    return local_time(v)


templates.env.globals["progress"] = _progress
templates.env.globals["msg"] = msg
templates.env.globals["js_messages"] = js_messages
templates.env.globals["app_name"] = APP_NAME
templates.env.globals["app_tagline"] = APP_TAGLINE
templates.env.globals["folder_list_stem"] = FOLDER_LIST_STEM
templates.env.filters["localtime"] = _localtime
templates.env.filters["folder_reason"] = folder_reason
templates.env.filters["reason_brief"] = reason_brief
templates.env.filters["stage_label"] = stage_label
templates.env.filters["badge_label"] = badge_label
templates.env.filters["error_text"] = error_text


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.home = real(settings.home)
        self.app_data = settings.data_dir
        self.db = DB(settings.db_path)
        self._flashes: OrderedDict[str, str] = OrderedDict()
        self._flash_lock = RLock()
        self.setup_lock = RLock()
        self.approval_key = secrets.token_bytes(32)

    @property
    def flash(self) -> str | None:
        browser = _browser.get()
        with self._flash_lock:
            return self._flashes.get(browser) if browser else None

    @flash.setter
    def flash(self, message: str | None) -> None:
        browser = _browser.get()
        if not browser:
            return
        with self._flash_lock:
            if message is None:
                self._flashes.pop(browser, None)
                return
            self._flashes[browser] = message
            self._flashes.move_to_end(browser)
            if len(self._flashes) > 128:
                self._flashes.popitem(last=False)

    def scope(self) -> Scope:
        return Scope(self.db.list_roots(), self.home, self.app_data)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.t = AppState(settings)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        # 다른 웹사이트가 로컬 서버로 요청을 보내는 것(CSRF)을 막는다.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin:
                o = urlparse(origin)
                if o.scheme != request.url.scheme or o.hostname != request.url.hostname or o.port != request.url.port:
                    return PlainTextResponse("cross-origin request blocked", status_code=403)
        browser = request.cookies.get(BROWSER_COOKIE, "")
        fresh = not re.fullmatch(r"[a-f0-9]{32}", browser)
        if fresh:
            browser = secrets.token_hex(16)
        token = _browser.set(browser)
        try:
            response = await call_next(request)
        finally:
            _browser.reset(token)
        if fresh:
            response.set_cookie(BROWSER_COOKIE, browser, httponly=True, samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        return response

    # 이전 즐겨찾기는 현재 설정 화면으로 연결한다. 이전 변경 API는 제공하지 않는다.
    @app.get("/s0", include_in_schema=False)
    def old_targets():
        return RedirectResponse("/setup/sources", status_code=303)

    @app.get("/s1", include_in_schema=False)
    def old_structure():
        return RedirectResponse("/setup/structure", status_code=303)

    from tongdory2.web.routes import app_settings, home, setup, s2, s2a, s3, s4

    app.include_router(app_settings.router)
    app.include_router(home.router)
    app.include_router(setup.router)
    app.include_router(s2.router)
    app.include_router(s2a.router)
    app.include_router(s3.router)
    app.include_router(s4.router)
    return app
