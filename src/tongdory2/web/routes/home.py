"""상황판(홈) — 지금 위치·완료한 것·다음 할 일 하나 (화면 기획 v2.3)."""

from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from tempfile import mkstemp

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tongdory2.fsx.paths import tilde

from tongdory2.catalog.store import CatalogStore
from tongdory2.liner import auto_consent
from tongdory2.liner.usage import RunUsage, has_api_history, run_usage
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import templates
from tongdory2.web.progress import state
from tongdory2.web.setup_state import settings_change_notice

router = APIRouter()
log = logging.getLogger(__name__)
API_LABEL = {
    "/decisions": "Decisions · 분류 판정", "/embeddings": "Embeddings · 문서와 폴더 비교",
    "/rerank": "Rerank · 후보 순위", "/tools/search/web": "Search · 공개 규격 검색",
    "/chat/completions": "Chat · 폴더 제안과 요약",
}


def _recent_liner_calls(conn) -> list[dict]:
    if not has_api_history(conn):
        return []
    rows = conn.execute("""
        SELECT endpoint, cost_kind, cost_usd, created_at
        FROM api_calls WHERE endpoint != '/credits/balance' ORDER BY rowid DESC LIMIT 30
    """)
    return [{"api": API_LABEL.get(endpoint, endpoint), "kind": kind, "cost": cost, "time": stamp}
            for endpoint, kind, cost, stamp in rows]


def _liner_totals(conn, run_id: str | None = None) -> dict:
    """표시 범위의 호출 수와 비용을 같은 호출 원장에서 집계한다."""
    if not has_api_history(conn):
        return {"external": 0, "cached": 0, "unreported": 0, "cost": 0}
    where = "endpoint != '/credits/balance'"
    params = ()
    if run_id is not None:
        where += " AND run_id=?"
        params = (run_id,)
    row = conn.execute(f"""
        SELECT COUNT(CASE WHEN cost_kind != 'CACHED' THEN 1 END),
               COUNT(CASE WHEN cost_kind = 'CACHED' THEN 1 END),
               COUNT(CASE WHEN cost_kind = 'NONE' THEN 1 END),
               COALESCE(SUM(CASE WHEN cost_kind != 'CACHED' THEN cost_usd ELSE 0 END), 0)
        FROM api_calls WHERE {where}
    """, params).fetchone()
    return dict(zip(("external", "cached", "unreported", "cost"), row))


def next_action(s: dict) -> dict:
    if s["pending_setup"] == "structure":
        return {"title": "분류 희망 폴더 목록 정하기", "desc": "기존 폴더나 목록 파일을 가져오거나 추천 목록을 불러와 원하는 대로 수정할 수 있습니다.",
                "href": "/setup/structure", "label": "분류 희망 폴더 목록 정하기"}
    if s["pending_setup"] == "sources":
        return {"title": "정리 대상 폴더 고르기", "desc": "원본 파일이 있는 위치를 선택하세요.",
                "href": "/setup/sources", "label": "정리 대상 폴더 고르기"}
    if s["pending_setup"] == "destination":
        return {"title": "저장 폴더 정하기", "desc": "정리된 파일을 모을 폴더 한 곳을 선택하세요.",
                "href": "/setup/destination", "label": "저장 폴더 정하기"}
    if s["running"]:
        return {"title": "분석 중입니다", "desc": "파일은 옮기지 않습니다.", "href": "/s2", "label": "진행 상황 보기 →"}
    if not s["analyzed"]:
        return {"title": "선택한 폴더 분석하기", "desc": "먼저 파일명과 경로로 이동할 폴더를 찾습니다.", "href": "/s2?new=1", "label": "파일 분석 시작"}
    if s["executed"]:
        title = "파일 이동 없이 검토를 마쳤습니다" if s["review_only"] else ("부분 정리를 마쳤습니다" if s["partial"] else "정리를 마쳤습니다")
        return {"title": title, "desc": "필요하면 인수인계 초안이나 지식센터 보관을 이어서 할 수 있습니다.", "href": "/s4", "label": "후속 작업 보기 →"}
    if s["counts"].get("check"):
        return {"title": f"확인이 필요한 {s['counts']['check']}개를 정하세요", "desc": "정하지 않은 파일은 원래 위치에 그대로 둡니다.", "href": "/s2?tab=check", "label": f"확인 필요 {s['counts']['check']}개 보기 →"}
    return {"title": "정리 계획을 확인하세요", "desc": "실행 전에 옮길 파일과 새 폴더를 모두 보여 드립니다.", "href": "/s2?tab=plan", "label": "정리 계획 만들기 →"}


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    t = request.app.state.t
    s = state(t)
    notice, t.flash = t.flash, None
    history = CatalogStore(t.db.conn).document_analysis_history()
    run_id = s["run"]["run_id"] if s["analyzed"] else None
    recent = next((event for event in history if event["run_id"] == run_id and event["plan_before"] is not None), None)
    extra = {}
    if s["executed"]:
        # 실행을 마친 뒤에는 분류 결과가 아니라 실제로 옮긴 결과를 오른쪽 폴더 목록에 보여 준다.
        from tongdory2.fsx.roots import Role
        from tongdory2.plan.executor import batch_ops
        from tongdory2.web.routes.s3 import result_tree_rows

        cur = next((r for r in t.db.list_roots() if r.role == Role.CURRENT and r.confirmed), None)
        ops = batch_ops(t.db.conn, s["last_exec"]["batch_id"])
        extra["tree_rows"] = result_tree_rows(ops, cur.path if cur else None, SpecStore(t.db.conn).current(), t.scope())
    return templates.TemplateResponse(request, "home.html", {
        **extra,
        "s": s, "na": next_action(s), "notice": notice, "liner_history": history, "recent_analysis": recent,
        "liner_calls": _recent_liner_calls(t.db.conn), "has_liner_key": bool(t.settings.liner_api_key),
        "liner_usage": run_usage(t.db.conn, run_id) if run_id else RunUsage(),
        "liner_totals": _liner_totals(t.db.conn),
        "latest_liner_totals": _liner_totals(t.db.conn, run_id) if run_id else None,
        "liner_auto": auto_consent.load(t.settings.data_dir),
        "ask_liner_consent": request.query_params.get("liner_consent") == "1",
    })


@router.get("/liner/usage.csv")
def liner_usage_csv(request: Request):
    t = request.app.state.t
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["시각", "API", "처리", "비용(추정값 포함)(USD)", "비용 유형"])
    if has_api_history(t.db.conn):
        rows = t.db.conn.execute("""
            SELECT created_at, endpoint, cost_kind, cost_usd
            FROM api_calls WHERE endpoint != '/credits/balance' ORDER BY rowid DESC LIMIT 5000
        """)
        for stamp, endpoint, kind, cost in rows:
            proc = "저장된 응답 재사용" if kind == "CACHED" else "외부 호출"
            cost_str = "캐시 사용" if kind == "CACHED" else ("비용 미기록" if kind == "NONE" else f"{cost or 0:.4f}")
            writer.writerow([stamp or "", API_LABEL.get(endpoint, endpoint), proc, cost_str, kind or ""])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="liner_usage.csv"'},
    )


def _clear_review_state(t) -> None:
    for name in ("job", "prep_job", "auto_job", "survey", "s2_back", "handover_job"):
        vars(t).pop(name, None)
    t.prepared = None


@router.post("/new-run")
def new_run(request: Request, confirm: str = Form("")):
    """R03 [새 정리 시작] — 지난 실행·Liner 기록은 그대로 두고 설정 확정만 풀어 처음 단계부터 다시 한다."""
    t = request.app.state.t
    if confirm != "yes":
        t.flash = "[새 정리 시작] 확인 체크를 해 주세요."
        return RedirectResponse("/", status_code=303)
    with t.setup_lock:
        if notice := settings_change_notice(t, "분류가 진행 중입니다. 멈춘 뒤 새 정리를 시작해 주세요."):
            t.flash = notice
            return RedirectResponse("/", status_code=303)
        SpecStore(t.db.conn)  # 새 DB에서 직접 요청해도 구조 버전 테이블을 준비한다
        t.db.restart_setup()
        _clear_review_state(t)
    t.flash = "기존 폴더 설정을 비웠습니다. 1단계(분류 희망 폴더 목록)부터 새로 설정해 주세요."
    return RedirectResponse("/", status_code=303)


@router.post("/demo-reset")
def demo_reset(request: Request, confirm: str = Form("")):
    """R03 [앱 기록 초기화] — 앱 기록을 모두 지운다. 지우기 전에 DB 사본을 backups/에 남긴다(내 PC 파일은 그대로)."""
    t = request.app.state.t
    if confirm != "yes":
        t.flash = "초기화 확인 체크를 해 주세요."
        return RedirectResponse("/", status_code=303)
    with t.setup_lock:
        if notice := settings_change_notice(t, "분류가 진행 중입니다. 멈춘 뒤 초기화해 주세요."):
            t.flash = notice
            return RedirectResponse("/", status_code=303)
        conn = t.db.conn
        backup_dir = Path(t.db.path).parent / "backups"
        dst_path = None
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            fd, name = mkstemp(prefix=f"tongdory2-{datetime.now():%Y%m%d-%H%M%S}-", suffix=".db", dir=backup_dir)
            dst_path = Path(name)
            os.close(fd)
            dst = sqlite3.connect(dst_path)
            try:
                conn.backup(dst)
            finally:
                dst.close()
        except (OSError, sqlite3.Error):
            if dst_path:
                try:
                    dst_path.unlink(missing_ok=True)
                except OSError:
                    log.warning("[기록초기화] 불완전한 사본 파일을 정리하지 못했습니다.")
            log.exception("[기록초기화] 백업 저장 실패")
            t.flash = "기록 사본을 저장할 수 없어 초기화를 중단했습니다."
            return RedirectResponse("/", status_code=303)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with conn:
                for name in tables:
                    conn.execute(f'DELETE FROM "{name}"')
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        _clear_review_state(t)
    t.flash = f"앱 기록을 초기화했습니다. 내 PC 파일은 바뀌지 않았습니다. (지우기 전 기록 사본: {tilde(dst_path, t.home)})"
    return RedirectResponse("/", status_code=303)
