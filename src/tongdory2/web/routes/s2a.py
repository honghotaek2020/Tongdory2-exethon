"""S2-A 제자리 목록화 화면 (A-01~A-05): 저장소 안 AI 협업 문서를 이동 없이 목록화."""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import threading
import traceback
from collections import Counter, defaultdict
from dataclasses import asdict

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from tongdory2.catalog.inplace import DOC_TYPE_LABEL, map_virtual, scan_inplace
from tongdory2.catalog.store import CatalogStore
from tongdory2.fsx.paths import tilde
from tongdory2.fsx.roots import STORAGE_EXPORT, Role
from tongdory2.handover.docs_index import ORDER, render
from tongdory2.liner.usage import run_usage
from tongdory2.plan.executor import write_new
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import templates
from tongdory2.web.routes.setup import _checks, _path
from tongdory2.web.setup_state import approval_token, approved, pending_setup_step, scope_key, settings_change_notice

router = APIRouter()
GIT_CHIP = {"TRACKED": ("ok", "Git 추적"), "UNTRACKED": ("warn", "미추적"), "IGNORED": ("danger", "Git 제외 · 보존 위험"),
            "NO_REPO": ("", "저장소 아님"), "UNKNOWN": ("", "?")}


def _ctx(request: Request):
    t = request.app.state.t
    roots = t.db.list_roots()
    spec = SpecStore(t.db.conn).current()
    pending = pending_setup_step(roots, spec)
    if pending:
        t.flash = "먼저 정리 설정을 완료해 주세요."
        return t, None, None, RedirectResponse(f"/setup/{pending}", status_code=303)
    return t, roots, spec, None


@router.get("/s2a", response_class=HTMLResponse)
def s2a_page(request: Request):
    return _render(request)


def _render(request: Request, *, error: str = "", warnings=None, target_paths: str | None = None,
            target_link: str | None = None):
    t, roots, spec, redirect = _ctx(request)
    if redirect:
        return redirect
    notice, t.flash = t.flash, None
    ips = [r for r in roots if r.role == Role.IN_PLACE]
    currents = [r for r in roots if r.role == Role.CURRENT and r.confirmed]
    job = getattr(t, "ip_job", None)
    if job and job.get("running"):
        return templates.TemplateResponse(request, "s2_progress.html", {"step": "s2", "job": job, "refresh_to": "/s2a"})
    run = CatalogStore(t.db.conn).latest("IN_PLACE")
    entries = CatalogStore(t.db.conn).entries(run["run_id"]) if run else []
    import json

    summary = json.loads(run["notice"]) if run and run.get("notice") else {}
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        groups[e.get("doc_type") or ("링크" if e.get("kind") != "text" else "OTHER")].append(e)
    return templates.TemplateResponse(request, "s2a.html", {
        "step": "s2", "notice": notice, "ips": [tilde(r.path, t.home) for r in ips], "run": run, "summary": summary,
        "groups": groups, "order": ORDER + ["링크"], "labels": DOC_TYPE_LABEL | {"링크": "링크·기타"},
        "git_chip": GIT_CHIP, "verdicts": Counter(e["verdict"] for e in entries),
        "liner_usage": run_usage(t.db.conn, run["run_id"]) if run else None,
        "error": error, "warnings": warnings or [],
        "target_paths": target_paths if target_paths is not None else "\n".join(tilde(r.path, t.home) for r in ips),
        "target_link": target_link if target_link is not None else "keep",
        "currents": [(r.root_id, tilde(r.path, t.home)) for r in currents],
        "revision": _ip_key(roots, spec),
    })


@router.post("/s2a/targets")
def save_targets(request: Request, paths: str = Form(""), linked: str = Form("keep"),
                 confirm_warnings: bool = Form(False), revision: str = Form("")):
    """협업 대상만 교체한다. 정리 대상·저장 폴더의 확정 상태는 유지한다."""
    t = request.app.state.t
    with t.setup_lock:
        t, roots, spec, redirect = _ctx(request)
        if redirect:
            return redirect
        if notice := settings_change_notice(t):
            t.flash = notice
            return RedirectResponse("/s2a", status_code=303)
        if revision and revision != _ip_key(roots, spec):
            return _render(request, error="설정이 바뀌었습니다. 현재 대상을 확인하고 다시 저장해 주세요.")
        try:
            selected = [_path(line, t) for line in paths.splitlines() if line.strip()]
            if len(selected) > 30:
                raise ValueError("협업 문서 대상 폴더는 한 번에 30곳까지 선택할 수 있습니다.")
            currents = {r.root_id for r in roots if r.role == Role.CURRENT and r.confirmed}
            if linked not in ("keep", "") and linked not in currents:
                raise ValueError("연결할 저장 폴더를 다시 선택해 주세요.")
            checks, warnings = _checks(selected, Role.IN_PLACE, [r for r in roots if r.role != Role.IN_PLACE],
                                       t, confirm_warnings)
            if warnings:
                return _render(request, warnings=warnings, target_paths=paths, target_link=linked)
            t.db.save_setup_roots(Role.IN_PLACE, checks)
            for root in t.db.list_roots():
                if root.role == Role.IN_PLACE:
                    link = root.linked_root_id if linked == "keep" else linked
                    t.db.set_link(root.root_id, link if link in currents else None)
            # 폴더나 연결이 바뀐 전송 미리보기는 승인할 수 없다.
            t.ip_pending = None
        except ValueError as error:
            return _render(request, error=str(error), target_paths=paths, target_link=linked)
    t.flash = "협업 문서 대상을 저장했습니다." if checks else "협업 문서 대상을 등록 해제했습니다. 실제 파일은 그대로입니다."
    return RedirectResponse("/s2a", status_code=303)


@router.post("/s2a/analyze")
def s2a_analyze(request: Request):
    t, roots, spec, redirect = _ctx(request)
    if redirect:
        return redirect
    ips = [r for r in roots if r.role == Role.IN_PLACE]
    if not ips:
        t.flash = "협업 문서 대상이 없습니다. [대상 폴더 설정]에서 먼저 등록해 주세요."
        return RedirectResponse("/s2a", status_code=303)
    if not all(r.confirmed for r in ips):
        t.flash = "[대상 폴더 설정]에서 협업 문서 대상을 확인하고 저장해 주세요."
        return RedirectResponse("/s2a", status_code=303)
    scope = t.scope()
    entries, summary = [], Counter()
    for r in ips:
        es, s = scan_inplace(r, scope, t.home)
        pre = r.root_id[-4:]
        for e in es:
            e.entry_id = f"{pre}{e.entry_id}"
            if e.backup_of:
                e.backup_of = f"{pre}{e.backup_of}"
        entries += es
        summary.update({k: v for k, v in s.items() if isinstance(v, int) and not isinstance(v, bool)})
    pending = [e for e in entries if e.verdict == "PENDING"]
    if not pending or not t.settings.liner_api_key:
        _start_inplace(t, spec, entries, summary, use_liner=False)
        return RedirectResponse("/s2a", status_code=303)
    # #60 Liner에 보내기 전에 전송 내용을 보여 주고 별도 승인을 받는다
    payload = json.dumps([(e.entry_id, e.payload) for e in pending], ensure_ascii=False)
    token = secrets.token_urlsafe(12)
    t.ip_pending = {"token": token, "entries": entries, "summary": summary, "payload": payload,
                    "key": _ip_key(roots, spec)}
    return templates.TemplateResponse(request, "s2a_preview.html", {
        "step": "s2", "ready": pending, "total": len(entries), "token": token,
        "approval": approval_token(t, "inplace", token, payload),
        "folders": [f"{n['segment']}: {n['description']}" for n in spec["body"]["nodes"]
                    if n["status"] == "ADOPTED" and n["role"] == "DEST" and n["profile"] in ("common", "build")],
    }, headers={"Cache-Control": "no-store"})


@router.post("/s2a/approve")
def s2a_approve(request: Request, token: str = Form(""), approval: str = Form(""), reviewed: str = Form(""),
                local_only: str = Form("")):
    t, roots, spec, redirect = _ctx(request)
    if redirect:
        return redirect
    pend = getattr(t, "ip_pending", None)
    if not pend or pend["token"] != token or pend["key"] != _ip_key(roots, spec):
        if pend and pend["token"] == token:
            t.ip_pending = None  # 대상이 바뀐 미리보기는 폐기
        t.flash = "전송 내용이 바뀌었거나 오래된 확인 화면입니다. [분석 시작]을 다시 눌러 주세요."
        return RedirectResponse("/s2a", status_code=303)
    if local_only == "1":
        t.ip_pending = None
        _start_inplace(t, spec, pend["entries"], pend["summary"], use_liner=False)
        return RedirectResponse("/s2a", status_code=303)
    if reviewed != "yes" or not approved(t, approval, "inplace", token, pend["payload"]):
        t.flash = "전송 내용을 확인했다는 체크가 필요합니다."
        return RedirectResponse("/s2a", status_code=303)
    t.ip_pending = None
    _start_inplace(t, spec, pend["entries"], pend["summary"], use_liner=True)
    return RedirectResponse("/s2a", status_code=303)


@router.post("/s2a/cancel")
def s2a_cancel(request: Request, token: str = Form("")):
    """#64 미리보기를 취소하면 승인 대기를 지운다 — 예전 확인 화면으로는 승인할 수 없다."""
    t = request.app.state.t
    pend = getattr(t, "ip_pending", None)
    if pend and pend["token"] == token:
        t.ip_pending = None
        t.flash = "전송을 취소했습니다. 아무것도 보내지 않았습니다."
    return RedirectResponse("/s2a", status_code=303)


def _ip_key(roots, spec) -> str:
    """#65 협업 승인 비교값: 정리 설정 + 확정된 협업(④) 대상."""
    ips = sorted((r.root_id, str(r.path), r.linked_root_id or "")
                 for r in roots if r.role == Role.IN_PLACE and r.confirmed)
    return scope_key(roots, spec) + "|" + json.dumps(ips, ensure_ascii=False)


def _start_inplace(t, spec, entries, summary, use_liner: bool) -> None:
    store = CatalogStore(t.db.conn)
    run_id = store.start(spec["version"], dict(summary), kind="IN_PLACE")
    job = {"running": True, "stage": "가상 매핑" if use_liner else "로컬 정리", "done": 0, "total": len(entries)}
    t.ip_job = job
    settings = t.settings

    def worker() -> None:
        from tongdory2.liner.client import LinerClient

        conn = sqlite3.connect(str(settings.db_path), check_same_thread=False)
        err, cost = None, 0.0
        try:
            if use_liner:
                async def go():
                    async with LinerClient(settings.liner_api_key, settings.liner_base_url, conn=conn, run_id=run_id) as c:
                        await map_virtual(entries, spec, c)
                        return c.total_cost_usd

                cost = asyncio.run(go())
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        for e in entries:
            if e.verdict in ("PENDING", "PENDING_BACKUP"):
                e.verdict, e.reason_code = "HOLD", "NOT_ANALYZED"
                if not use_liner:
                    e.reason = "Liner에 보내지 않음(내 PC 분류 결과만) — 다시 분석해 전송을 승인하면 폴더를 매핑합니다"
        CatalogStore(conn).finish(run_id, [asdict(e) for e in entries], cost, err)
        conn.close()
        job["running"] = False

    threading.Thread(target=worker, daemon=True).start()


@router.post("/s2a/index")
def s2a_index(request: Request):
    import json

    t, roots, spec, redirect = _ctx(request)
    if redirect:
        return redirect
    run = CatalogStore(t.db.conn).latest("IN_PLACE")
    if not run:
        return RedirectResponse("/s2a", status_code=303)
    entries = CatalogStore(t.db.conn).entries(run["run_id"])
    summary = json.loads(run["notice"] or "{}")
    ip = next((r for r in roots if r.role == Role.IN_PLACE and r.confirmed), None)
    if ip is None:  # #66 협업 대상을 등록 해제한 뒤 예전 화면에서 누른 경우
        t.flash = "협업 문서 대상이 등록되어 있지 않아 색인을 만들 수 없습니다. 대상을 등록하고 다시 분석해 주세요."
        return RedirectResponse("/s2a", status_code=303)
    by_id = {r.root_id: r for r in roots}
    linked = by_id.get(ip.linked_root_id) if ip.linked_root_id else None
    storage = next((r for r in roots if r.role == Role.STORAGE), None)
    # 결정 #21·#39: 연결 ② 과제 루트에 새 파일, 없으면 ⑤ 03.내보내기
    target_dir = linked.path if linked else storage.path / STORAGE_EXPORT
    content = render(entries, summary | {"md_total": summary.get("md_total", 0)}, ip.path, target_dir,
                     linked.path.name if linked else ip.path.name)
    try:
        _, path = write_new(t.db.conn, target_dir / "README_문서색인.md", content, t.scope(), t.home, "문서 색인")
    except PermissionError as exc:
        t.flash = f"색인을 쓸 수 없습니다: {exc}"
        return RedirectResponse("/s2a", status_code=303)
    t.flash = f"문서 색인을 만들었습니다. {tilde(path, t.home)} · 작업 기록에서 되돌릴 수 있습니다."
    return RedirectResponse("/s2a", status_code=303)
