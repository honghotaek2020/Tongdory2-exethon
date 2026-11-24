"""앱 설정: Liner API 키 입력·변경·삭제, 앱 종료 (창 없는 배포본에서도 화면에서 다룬다)."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from tongdory2.config import reset_settings_cache
from tongdory2.instance import busy_jobs
from tongdory2.liner import auto_consent, keystore
from tongdory2.liner.client import LinerClient, LinerError
from tongdory2.web.app import templates

router = APIRouter()
log = logging.getLogger(__name__)


def safe_return(value: str) -> str:
    """키 저장 뒤 돌아갈 앱 안의 화면 주소만 허용한다 (다른 사이트로 보내는 주소는 버림)."""
    try:
        ref = urlparse(value or "")
    except ValueError:
        return ""
    if (ref.scheme or ref.netloc or not value.startswith("/") or value.startswith("//") or "\\" in value
            or len(value) > 1000 or value.startswith("/settings/liner")):
        return ""
    return value


VERIFY_TIMEOUT_S = 10.0


async def verify_key(key: str, base_url: str) -> tuple[str, float | None]:
    """요금이 들지 않는 잔액 조회로 키를 확인한다. 결과: ok | invalid | unreachable, 잔액(USD)."""
    try:
        async with LinerClient(key, base_url) as client:  # conn 없음: 사용 기록·캐시에 남기지 않는다
            balance = await asyncio.wait_for(client.credits_usd(), VERIFY_TIMEOUT_S)
    except LinerError as exc:
        if exc.status in (401, 403):
            return "invalid", None
        log.info("[설정] Liner 키 확인 실패: status=%s code=%s", exc.status, exc.code)
        return "unreachable", None
    except (asyncio.TimeoutError, OSError):
        return "unreachable", None
    return "ok", balance


def _key_page(return_to: str) -> str:
    return "/settings/liner" + (f"?return_to={quote(return_to, safe='')}" if return_to else "")


def key_status(t) -> keystore.KeyStatus:
    return keystore.status(keystore.key_file(t.settings.data_dir))


def _apply(t) -> keystore.KeyStatus:
    """저장·삭제 결과를 실행 중인 앱에 바로 반영한다 (다시 시작하지 않아도 된다)."""
    current = key_status(t)
    t.settings.liner_api_key = current.key
    reset_settings_cache()
    return current


@router.get("/settings/liner", response_class=HTMLResponse)
def liner_key_page(request: Request, return_to: str = ""):
    t = request.app.state.t
    notice, t.flash = t.flash, None
    return templates.TemplateResponse(request, "liner_key.html", {
        "notice": notice, "key": key_status(t),
        "return_to": safe_return(return_to),
    })


@router.post("/settings/liner")
async def liner_key_save(request: Request, action: str = Form("save"), key: str = Form(""), return_to: str = Form("")):
    t = request.app.state.t
    back = safe_return(return_to)
    path = keystore.key_file(t.settings.data_dir)
    if action == "delete":
        keystore.write(path, None)
        current = _apply(t)
        log.info("[설정] 화면에서 저장한 Liner 키를 삭제했습니다. 현재 키=%s", current.label)
        t.flash = "저장한 키를 삭제했습니다." + (" 환경 변수의 키는 계속 사용합니다." if current.key else "")
        return RedirectResponse(_key_page(back), status_code=303)
    try:
        value = keystore.validate(key)
    except ValueError as exc:
        t.flash = str(exc)
        return RedirectResponse(_key_page(back), status_code=303)
    result, balance = await verify_key(value, t.settings.liner_base_url)
    if result == "invalid":
        log.info("[설정] Liner가 거부한 키라 저장하지 않았습니다 (%s).", keystore.mask(value))
        t.flash = "Liner가 이 키를 거부했습니다. 키를 다시 확인해 주세요. 저장하지 않았습니다."
        return RedirectResponse(_key_page(back), status_code=303)
    keystore.write(path, value)
    current = _apply(t)
    checked = (f" 키를 확인했습니다(잔액 ${balance:,.2f})." if result == "ok" and balance is not None
               else " Liner에 연결하지 못해 키를 확인하지는 못했습니다. 분석할 때 다시 확인됩니다." if result == "unreachable"
               else "")
    log.info("[설정] Liner 키를 저장했습니다 (%s). 현재 키=%s", keystore.mask(value), current.label)
    if current.source != "app":
        t.flash = "키를 저장했지만 환경 변수 LINER_API_KEY가 먼저 적용됩니다. 저장한 키를 쓰려면 환경 변수를 지우고 다시 실행하세요."
        return RedirectResponse(_key_page(back), status_code=303)
    if back:  # 키 등록을 시작한 화면으로 돌아가 바로 Liner 분석을 이어 한다
        t.flash = "API 키를 저장했습니다." + checked + " 이제 Liner로 분석할 수 있습니다."
        return RedirectResponse(back, status_code=303)
    t.flash = "API 키를 저장했습니다." + checked
    return RedirectResponse("/settings/liner", status_code=303)


@router.post("/settings/liner-auto")
def liner_auto_save(request: Request, choice: str = Form(...), next_url: str = Form("", alias="next")):
    """파일 분석 단계의 Liner 자동 폴더 추천 동의를 저장한다 (최초 팝업 또는 Liner 사용 현황 체크박스).
    on: 동의 → 키가 없으면 키 등록 화면으로 안내 · off: 자동 추천 안 함 (수동 요청은 그대로)."""
    t = request.app.state.t
    back = safe_return(next_url) or "/"
    if choice not in ("on", "off"):
        return RedirectResponse(back, status_code=303)
    agreed = choice == "on"
    auto_consent.save(t.settings.data_dir, agreed)
    log.info("[설정] Liner 자동 폴더 추천 %s", "동의" if agreed else "사용 안 함")
    if not agreed:
        t.flash = ("Liner 자동 폴더 추천을 사용하지 않습니다. 확인 목록에서 파일을 골라 직접 요청할 수 있습니다."
                   if back == "/" else None)
        return RedirectResponse(back, status_code=303)
    if not t.settings.liner_api_key:
        t.flash = ("Liner 자동 폴더 추천에 동의했습니다. Liner API 키를 등록하면 파일 분석 때 자동으로 적용됩니다. "
                   "키 없이 진행하려면 '이전 화면으로 돌아가기'를 누르세요(이번 분석은 내 PC 규칙만 사용).")
        return RedirectResponse(_key_page(back), status_code=303)
    t.flash = "Liner 자동 폴더 추천을 켰습니다. 다음 파일 분석부터 적용됩니다." if back == "/" else None
    return RedirectResponse(back, status_code=303)


@router.post("/app/quit", response_class=HTMLResponse)
def quit_app(request: Request):
    t = request.app.state.t
    if busy_jobs(request.app):
        t.flash = "분석이나 정리 작업이 진행 중이라 종료할 수 없습니다. 작업이 끝난 뒤 다시 눌러 주세요."
        return RedirectResponse(request.headers.get("referer") or "/", status_code=303)
    server = getattr(request.app.state, "server", None)
    if server is None:
        t.flash = "이 실행 방식에서는 화면에서 종료할 수 없습니다. 실행한 터미널에서 Ctrl+C로 종료하세요."
        return RedirectResponse(request.headers.get("referer") or "/", status_code=303)
    log.info("[종료] 화면의 [앱 종료]로 종료합니다.")
    server.should_exit = True  # 이 응답을 보낸 뒤 정상 종료
    return templates.TemplateResponse(request, "quit.html", {})
