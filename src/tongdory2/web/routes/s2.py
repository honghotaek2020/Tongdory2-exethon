"""S2 목록화와 사용자 요청 문서 분석, 이미지 및 문서 미리보기.

파일은 하나도 옮기지 않는다. 뷰어는 로컬 서버 → 내 브라우저 표시(외부 전송 0).
클라이언트는 entry_id만 보낸다 (경로를 받지 않음).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import mimetypes
import secrets
import os
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from dataclasses import fields
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from tongdory2.catalog import pipeline
from tongdory2.catalog.inventory import InventoryCheck, InventoryStore, completed_item
from tongdory2.catalog.pipeline import (
    Entry, Prepared, analyze, classify_by_names, document_candidate, document_folder_text,
    document_folders, entries_to_dicts, load_document, name_candidate, name_payload, prepare,
)
from tongdory2.catalog.grouping import organize_rate, apply_groups
from tongdory2.catalog.store import CatalogStore
from tongdory2.fsx.paths import tilde
from tongdory2.catalog.survey import Exclusions, FilterStore, SENSITIVE_EXTS, run_survey
from tongdory2.fsx.roots import Role
from tongdory2.fsx.scope import Access, Scope
from tongdory2.liner import auto_consent
from tongdory2.messages import reason_brief
from tongdory2.web.progress import needs_approval as _needs_approval
from tongdory2.liner.usage import run_usage
from tongdory2.plan.planner import missing_destination_folders
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import templates
from tongdory2.web.selection_form import read_selection
from tongdory2.web.setup_state import approval_token, approved, current_run, pending_setup_step, scope_key
from tongdory2.web.series import count_selected, current_series, done_runs, notice_of, series_run, view_run

router = APIRouter()
log = logging.getLogger(__name__)

VERDICT_LABEL = {
    "OVERRIDE": "직접 지정",
    "JOIN": "기존 폴더에 합류",
    "SPEC_NODE": "목록에 있는 폴더를 만든 뒤 이동",
    "NEW_NODE": "새 폴더 제안",
    "GROUP": "묶음 새 폴더",
    "ETC": "기타",
    "HOLD": "보류",
    "EXCLUDE": "제외",
    "DUP": "중복",
}
# 확인 필요 탭: 직접 확인(HOLD, 폴더 지정·Liner 수동 요청 막대)을 새 폴더 승인보다 먼저 보인다
ORDER = ["OVERRIDE", "JOIN", "SPEC_NODE", "GROUP", "NEW_NODE", "APPROVE", "HOLD", "ETC", "DUP", "EXCLUDE"]
USER_LABEL = {  # 화면 기획 v2.3: 내부 판정 코드 대신 사용자 말
    "OVERRIDE": "직접 지정한 파일",
    "JOIN": "기존 폴더로 정리",
    "SPEC_NODE": "필요한 폴더를 만든 뒤 정리",
    "NEW_NODE": "Liner 새 폴더 추천",
    "APPROVE": "승인 필요",
    "GROUP": "원래 폴더·파일 이름으로 묶어 새 폴더로",
    "ETC": "낱개 파일 — 기타 폴더 또는 Liner에 묻기",
    "HOLD": "확인 필요",
    "DUP": "중복 후보",
    "EXCLUDE": "안전을 위해 그대로 둠",
}


def _assigned(e: dict) -> bool:
    return bool(e.get("override")) and e["override"] != "CLEAR"


def _t(request: Request):
    return request.app.state.t


def _guard(request: Request):
    t = _t(request)
    roots = t.db.list_roots()
    spec = SpecStore(t.db.conn).current()
    if pending := pending_setup_step(roots, spec):
        if pending == "structure":
            t.flash = "분류 희망 폴더 목록을 먼저 확정해 주세요."
        return None, None, RedirectResponse(f"/setup/{pending}", status_code=303)
    return roots, spec, None


@router.get("/s2", response_class=HTMLResponse)
def s2_page(request: Request):
    roots, spec, redirect = _guard(request)
    if redirect:
        return redirect
    t = _t(request)
    store = CatalogStore(t.db.conn)
    q = request.query_params
    job = getattr(t, "job", None)
    notice = t.flash or (job or {}).get("message")
    liner_rows = (job or {}).get("result_rows") if notice and not t.flash else None
    t.flash = None
    result = view_run(store, roots, spec, q.get("batch")) if q.get("result") else None
    document_job = bool(job and job.get("ticket") and job["ticket"] == q.get("document_job"))

    if job and job.get("running") and (document_job or not result):
        return templates.TemplateResponse(request, "s2_progress.html", {"step": "s2", "job": job, "local": job.get("local"),
                                                                        "liner_auto": auto_consent.load(t.settings.data_dir),
                                                                        "refresh_to": str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
                                                                        "previous_result": job.get("return_to")})

    if document_job:
        return RedirectResponse(f"{job['return_to']}&liner_result={job['ticket']}", status_code=303)
    pjob = getattr(t, "prep_job", None)
    if pjob and pjob.get("running") and not result:
        return templates.TemplateResponse(request, "s2_progress.html", {
            "step": "s2", "job": pjob, "local": True, "elapsed": int(time.time() - pjob.get("started", time.time())),
            "liner_auto": auto_consent.load(t.settings.data_dir),
            "previous_result": "/s2?result=1" if view_run(store, roots, spec, None) else None})
    sv = getattr(t, "survey", None)
    if not result and pjob and pjob.get("awaiting_filter") and sv and sv["key"] == scope_key(roots, spec):
        saved = FilterStore(t.db.conn).load()
        chosen = sv.get("rejected_filter") or saved
        excluded = chosen.exts if chosen else {e["ext"] for e in sv["survey"].exts if e["sensitive"]}
        return templates.TemplateResponse(request, "s2_filter.html", {
            "step": "s2", "s": sv["survey"], "token": sv["token"], "excluded": excluded,
            "excluded_dirs": chosen.dirs if chosen else set(), "one_batch": ONE_BATCH_MAX, "notice": notice})
    latest = store.latest()
    run = result or view_run(store, roots, spec, q.get("batch")) or latest
    prep = getattr(t, "prepared", None)
    key = scope_key(roots, spec)
    if q.get("new") and not (prep and prep.notice.get("scope_key") == key) and not _auto_running(t):
        if auto_consent.load(t.settings.data_dir) is None:
            # 최초 1회: Liner 자동 폴더 추천 동의를 상황판 팝업에서 묻고, 답한 뒤 이 주소로 다시 온다
            return RedirectResponse("/?liner_consent=1", status_code=303)
        _start_prepare(t, roots, spec, resume_unapproved=latest is None)
        return RedirectResponse("/s2", status_code=303)
    if prep and prep.notice.get("scope_key") == key and not result:
        if not prep.entries and prep.notice.get("after_run"):
            return templates.TemplateResponse(request, "s2_retry.html", {
                "notice": ("미분석 파일을 찾지 못했습니다. 접근할 수 없는 폴더 또는 탐색 깊이 제한이 있습니다."
                           if prep.notice["coverage_unknown"] else "현재 폴더에 다음 묶음으로 분석할 파일이 없습니다."),
                "previous_run": prep.notice["after_run"],
            })
        return templates.TemplateResponse(request, "s2_notice.html", {
            "step": "s2", "n": prep.notice, "notice": notice,
            "prep_token": pjob["token"],
        })
    if not current_run(run, roots, spec):
        retry_run = None
        old_notice = json.loads(run["notice"] or "{}") if run else {}
        if run and old_notice.get("scope_key") == key and run["status"] in ("FAILED", "RUNNING"):
            retry_run = run["run_id"]
            notice = ("파일이 바뀌었습니다. 이 묶음을 다시 분류하고 전송 내용을 확인해 주세요."
                      if run["error"] == "FILES_CHANGED" else "이 묶음의 분석을 마치지 못했습니다. 다시 시도할 수 있습니다.")
        if pjob and pjob.get("error") and pjob["scope_key"] == key:
            notice = "파일 분류 중 오류가 발생했습니다. 폴더에 접근할 수 있는지 확인해 주세요."
        if notice or pjob or run:
            return templates.TemplateResponse(request, "s2_retry.html", {
                "notice": notice or "설정이 변경되었습니다. 파일을 다시 분석해 주세요.", "retry_run": retry_run,
            })
        _start_prepare(t, roots, spec, resume_unapproved=True)
        return RedirectResponse("/s2", status_code=303)

    t.s2_back = _review_url(request.url.path + "?" + request.url.query)
    store.refresh_defaults(run["run_id"])  # 예전 기준으로 저장된 결과의 '기본 선택'을 지금 기준으로
    entries = store.entries(run["run_id"])
    from tongdory2.web.progress import bucket as _bucket

    tab = request.query_params.get("tab") or ""
    tab_of = {"OVERRIDE": "plan", "JOIN": "plan", "SPEC_NODE": "plan", "GROUP": "plan", "NEW_NODE": "plan", "APPROVE": "check",
              "ETC": "check", "HOLD": "check", "DUP": "keep", "EXCLUDE": "keep"}
    all_groups: dict[str, list[dict]] = defaultdict(list)
    batches: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        if e.get("screenshot_batch") and e["verdict"] == "HOLD" and not _assigned(e):
            batches[e["screenshot_batch"]].append(e)
        elif _assigned(e):
            all_groups["OVERRIDE"].append(e)  # #8 직접 지정한 파일은 상황판과 같이 '정리 예정'에
        elif _needs_approval(e):
            all_groups["APPROVE"].append(e)  # 추천 폴더는 있지만 사람이 승인해야 옮기는 파일
        else:
            all_groups[e["verdict"]].append(e)
    if tab not in ("plan", "check", "keep"):
        tab = "check" if any(_bucket(e) == "check" for e in entries) else "plan"
    # 이번 탭의 항목만, PAGE_SIZE씩 (스크린샷 묶음은 확인 필요 첫 쪽에)
    view_batch = notice_of(run).get("batch_number", 1)
    tab_rows = [(v, e) for v in ORDER if tab_of[v] == tab for e in all_groups[v]]
    pages = max(1, -(-len(tab_rows) // PAGE_SIZE))
    try:
        page = min(max(1, int(request.query_params.get("page", 1))), pages)
    except ValueError:
        page = 1
    groups: dict[str, list[dict]] = defaultdict(list)
    for v, e in tab_rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]:
        groups[v].append(e)
    checked_now = store.effective_selection(run["run_id"], entries)
    new_folders = _new_folders(roots, entries, checked_now)
    review_url = _review_url(f"/s2?batch={view_batch}&tab={tab}&page={page}")
    group_totals = {v: len(all_groups[v]) for v in ORDER}
    approve_count = len(all_groups["APPROVE"])
    if tab != "check" or page != 1:
        batches = {}
    nodes = [n for n in spec["body"]["nodes"] if n["status"] == "ADOPTED" and n["role"] == "DEST"
             and n["profile"] in ("common", "build")]
    counts = Counter(e["verdict"] for e in entries)
    from tongdory2.web.progress import bucket, requires_liner_selection

    tabn = Counter(bucket(e) for e in entries)
    group_cards = _group_cards(entries, checked_now)
    new_node_folders = Counter(e.get("node_path") or "" for e in all_groups["NEW_NODE"])
    new_node_checked = sum(e["entry_id"] in checked_now for e in all_groups["NEW_NODE"])
    if job and not job.get("running") and notice == job.get("message"):
        job.pop("message", None)  # 완료 안내는 한 번만 띄운다 (다른 화면에서 돌아와도 다시 뜨지 않게)
        job.pop("result_rows", None)
    return templates.TemplateResponse(
        request,
        "s2.html",
        {
            "step": "s2", "notice": notice, "run": run, "groups": groups, "order": ORDER, "labels": VERDICT_LABEL,
            "new_node_folders": sorted(new_node_folders.items(), key=lambda kv: (-kv[1], kv[0])),
            "new_node_checked": new_node_checked, "approve_count": approve_count,
            "plan_checked": sum(1 for e in entries if e["entry_id"] in checked_now and bucket(e) == "plan"),
            "liner_rows": liner_rows,
            "batches": dict(sorted(batches.items(), reverse=True)), "counts": counts, "total": len(entries),
            "nodes": nodes, "spec_version": spec["version"], "tabn": tabn, "tab": tab, "ulabels": USER_LABEL,
            "tab_of": tab_of, "page": page, "pages": pages, "page_size": PAGE_SIZE, "group_totals": group_totals,
            "checked_now": checked_now, "n_checked": len(checked_now), "view_batch": view_batch,
            "review_url": review_url, "review_query": urlencode({"return_to": review_url}),
            "new_folder_count": len(new_folders), "new_folder_files": sum(map(len, new_folders.values())),
            "prep_in_progress": bool(pjob and (pjob.get("running") or pjob.get("awaiting_filter"))),
            "liner_suggestion_link": bool(job and job.get("suggested") and job.get("ticket") == q.get("liner_result")),
            "bar": _series_bar(t, store, roots, spec),
            "n_checked_all": count_selected(store, [r["run_id"] for r in done_runs(store, roots, spec)]),
            "node_paths": {n["node_id"]: n["display_path"] for n in spec["body"]["nodes"]},
            "analysis": json.loads(run["notice"] or "{}"),
            "liner_usage": run_usage(t.db.conn, run["run_id"]),
            "liner_review_ids": {e["entry_id"] for e in entries if requires_liner_selection(e)},
            "document_ids": {e["entry_id"] for e in entries if document_candidate(e)},
            "has_key": bool(t.settings.liner_api_key),
            "group_cards": group_cards, "rate": organize_rate(counts),
            "etc_liner_ids": [e["entry_id"] for e in entries if e["verdict"] == "ETC" and document_candidate(e)],
            "dups": _dup_rows(entries, checked_now, t.home),
        },
    )


ONE_BATCH_MAX = 10000  # 로컬 검사 한 묶음 최대 (남은 수가 이보다 적으면 남은 전부를 한 번에)
AUTO_CONTINUE = True  # 첫 묶음 저장 뒤 남은 묶음을 뒤에서 이어서 로컬 검사
PAGE_SIZE = 200        # 결과 화면은 탭마다 이만큼씩 나눠 보여 준다 (체크 상태는 서버에 저장)


def _batch_limit(t, pending: int) -> int:
    # 로컬 검사는 파일을 옮기지도 보내지도 않으므로 크게 한다: 남은 전부 또는 최대 1만 개
    return t.settings.batch_size or max(pipeline.MAX_ITEMS, min(pending, ONE_BATCH_MAX))


def _build_batch(t, roots, spec, scope, key, series_id, previous, batch, refresh, filtered_out, stage, progress):
    """목록에서 이번 묶음을 골라 로컬 검사한다 (DB 저장 전). 첫 묶음과 자동 이어 검사가 같이 쓴다."""
    catalog = CatalogStore(t.db.conn)
    inventory = InventoryStore(t.db.conn)
    previous_notice = json.loads(previous["notice"] or "{}") if previous else {}
    pending = inventory.pending_count(series_id)
    limit = _batch_limit(t, pending)
    stage("이번 묶음 확인", 0, min(limit, pending))
    selection = inventory.select(series_id, scope, limit, stage=stage)
    stage("로컬 분류", 0, len(selection.items))
    prep = prepare(roots, scope, spec, t.home, t.settings.recent_hours, progress,
                   batch=(selection.items, selection.coverage))
    reused = catalog.reuse_document_suggestions(prep)
    if reused:
        prep.notice["local_suggested"] += reused
        prep.notice["needs_review"] -= reused
    prep.notice["local_suggested"] = sum(e.verdict in ("JOIN", "SPEC_NODE") for e in prep.entries)
    _auto_names(t, prep, spec, progress)
    # 계획한 폴더에 맞지 않은 파일은 임의 '자동분류/...' 폴더를 만들지 않고 확인 필요(HOLD)에 남긴다.
    prep.notice.update(grouped_folder=0, grouped_name=0, grouped_etc=0, group_count=0,
                       needs_review=sum(e.verdict == "HOLD" for e in prep.entries),
                       images_held=sum(e.verdict == "HOLD" and e.kind == "image" for e in prep.entries))
    prep.notice.update(scope_key=key, series_id=series_id, batch_number=batch,
                       after_run=previous["run_id"] if previous else None,
                       prior_analyzed=selection.completed, prior_errors=previous_notice.get("total_errors", 0),
                       inventory_skipped=selection.skipped, inventory_refreshed=refresh, reused_documents=reused,
                       filtered_out=filtered_out, batch_limit=limit)
    return prep


AUTO_LINER_MAX = 2000  # 한 묶음에서 자동으로 Liner에 보내는 최대 파일 수 (넘는 파일은 수동 요청으로)


def _auto_names(t, prep, spec, progress: dict) -> None:
    """동의(최초 1회)했고 키가 있으면, 로컬 규칙으로 못 정한 파일의 상위 폴더명·파일명을 Liner에 보내
    1차-1 기존 폴더 매칭 → 1차-2 새 폴더 이름 추천을 받는다. 본문·민감 파일은 보내지 않는다."""
    from tongdory2.liner.client import LinerClient

    notice = prep.notice
    notice.update(auto_liner="off", liner_matched=0, liner_new=0, auto_liner_cost=0.0)
    if auto_consent.load(t.settings.data_dir) is not True:
        return
    candidates = [e for e in prep.entries if name_candidate(
        {"verdict": e.verdict, "override": getattr(e, "override", None), "sensitivity": e.sensitivity})]
    if not candidates:
        notice["auto_liner"] = "none"
        return
    if not t.settings.liner_api_key:
        notice["auto_liner"] = "no_key"
        return
    ready = _stage_names(candidates[:AUTO_LINER_MAX])
    notice["auto_liner_skipped"] = max(0, len(candidates) - AUTO_LINER_MAX)
    tag = f"prep:{secrets.token_hex(8)}"  # 묶음 저장 전이라 임시 꼬리표로 기록하고, 저장할 때 run_id로 바꾼다
    cost = 0.0
    conn = sqlite3.connect(str(t.settings.db_path), timeout=10)

    async def go():
        nonlocal cost
        async with LinerClient(t.settings.liner_api_key, t.settings.liner_base_url, conn=conn, run_id=tag) as client:
            try:
                await pipeline.classify_by_names(ready, spec, prep.current_root.path, client, progress)
            finally:
                cost = client.total_cost_usd

    try:
        asyncio.run(go())
        notice["auto_liner"] = "done"
    except Exception:
        log.exception("[자동추천] 파일명 Liner 분석 중 오류")
        notice["auto_liner"] = "error"
    finally:
        conn.close()
    for e in ready:
        if e.verdict == "PENDING":
            e.verdict, e.reason_code = "HOLD", "NOT_ANALYZED"
            e.reason = "자동 파일명 분석을 마치지 못했습니다. 확인 목록에서 다시 요청하거나 직접 지정해 주세요."
    notice.update(auto_liner_tag=tag, auto_liner_cost=cost, send_names=notice.get("send_names", 0) + len(ready),
                  liner_matched=sum(e.reason_code == "LINER_NAME_MATCH" for e in ready),
                  liner_new=sum(e.reason_code == "LINER_NAME_NEW" for e in ready),
                  auto_liner_errors=sum(e.reason_code == "NOT_ANALYZED" for e in ready))
    log.info("[자동추천] 전송 %d건 · 기존 폴더 %d건 · 새 폴더 %d건 · 비용 $%.4f",
             len(ready), notice["liner_matched"], notice["liner_new"], cost)


def _save_batch(conn, run_id: str, prep, scope, job) -> str | None:
    """검사 결과를 저장한다. 저장 직전 파일이 바뀌었으면 결과를 버리고 FILES_CHANGED."""
    err = None
    entries = prep.entries
    job.update(stage="분류한 파일 확인", done=0, total=len(prep.items))
    check = InventoryCheck(scope)
    for i, item in enumerate(prep.items, 1):
        if check.current(item) != item:
            err, entries = "FILES_CHANGED", []
            log.info("[목록화] 저장 전 파일 변경 감지, 묶음 재검사 필요")
            break
        job["done"] = i
    notice = prep.notice | {"analysis_errors": 0, "total_errors": prep.notice["prior_errors"]}
    if notice.get("auto_liner_tag"):  # 자동 추천 호출 기록을 이 묶음에 붙인다 (사용 현황·비용 집계)
        with conn:
            conn.execute("UPDATE api_calls SET run_id=? WHERE run_id=?", (run_id, notice["auto_liner_tag"]))
    CatalogStore(conn).finish(run_id, entries_to_dicts(entries), notice.get("auto_liner_cost") or 0.0, err,
                              notice=notice, inventory_items=prep.items)
    log.info("[목록화] 묶음 %d 결과 %d건", prep.notice["batch_number"], len(entries))
    return err


def _auto_running(t) -> bool:
    return bool(getattr(t, "auto_job", {}).get("running"))


def _start_auto(t) -> bool:
    """현재 시리즈의 남은 묶음을 한 번에 하나씩 순서대로 로컬 검사한다(옮기지도 보내지도 않음).
    설정이 바뀌거나, 파일이 바뀐 묶음이 생기거나, 사용자가 멈추면 멈춘다."""
    with t.setup_lock:
        if _auto_running(t) or getattr(t, "prep_job", {}).get("running"):
            return False
        roots = t.db.list_roots()
        spec = SpecStore(t.db.conn).current()
        store = CatalogStore(t.db.conn)
        sid, runs = current_series(store, roots, spec)
        done = done_runs(store, roots, spec)
        if not sid or not done or runs[-1]["status"] != "DONE":
            return False  # 실패·진행 중 묶음이 끝에 있으면 그 묶음부터 다시(재시도 화면)
        last = done[-1]
        if not notice_of(last).get("truncated"):
            return False
        key = scope_key(roots, spec)
        job = {"running": True, "stop": False, "series_id": sid, "key": key, "stage": "다음 묶음 준비",
               "done": 0, "total": 0, "batch_number": notice_of(last)["batch_number"] + 1,
               "started": time.time(), "reason": ""}
        t.auto_job = job
        scope = Scope(roots, t.home, t.app_data)

    def worker() -> None:
        previous = last
        try:
            store = CatalogStore(t.db.conn)
            while not job["stop"]:
                with t.setup_lock:
                    if scope_key(t.db.list_roots(), SpecStore(t.db.conn).current()) != key:
                        job["reason"] = "정리 설정이 바뀌어 남은 묶음 분류를 멈췄습니다."
                        break
                if InventoryStore(t.db.conn).pending_count(sid) == 0:
                    break
                batch = notice_of(previous)["batch_number"] + 1
                job.update(batch_number=batch, batch_started=time.time())

                def stage(name, done, total):
                    job.update(stage=name, done=done, total=total)

                prep = _build_batch(t, roots, spec, scope, key, sid, previous, batch, False, 0, stage, job)
                if not prep.items:
                    break
                run_id = store.start(spec["version"], prep.notice)
                err = _save_batch(t.db.conn, run_id, prep, scope, job)
                if err:
                    job["reason"] = f"{batch}번째 묶음 분류 중 파일이 바뀌어 멈췄습니다. 그 묶음을 다시 분류해 주세요."
                    break
                previous = store.get(run_id)
                log.info("[자동검사] 묶음 %d 완료 · 남은 %d건", batch, prep.notice.get("truncated", 0))
                if not prep.notice.get("truncated"):
                    break
            if job["stop"] and not job["reason"]:
                job["reason"] = "남은 묶음 분류를 멈췄습니다. [이어서 분류]로 계속할 수 있습니다."
        except Exception:
            job["reason"] = "남은 묶음 분류 중 오류가 발생했습니다. [이어서 분류]로 다시 시도할 수 있습니다."
            log.exception("[자동검사] 실패")
        finally:
            job["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return True


def _series_bar(t, store, roots, spec) -> dict:
    """묶음 표시줄: 완료(열람 가능)·검사 중·대기·실패."""
    sid, runs = current_series(store, roots, spec)
    auto = getattr(t, "auto_job", {}) or {}
    items = []
    for r in runs:
        n = notice_of(r)
        ok = r["status"] == "DONE" and current_run(r, roots, spec)
        items.append({"n": n.get("batch_number", 1), "state": "done" if ok else ("failed" if r["status"] == "FAILED" else
                      "running" if r["status"] == "RUNNING" else "stale"), "count": n.get("scanned", 0), "run_id": r["run_id"]})
    running = auto.get("running") and auto.get("series_id") == sid
    pending = InventoryStore(t.db.conn).pending_count(sid) if sid else 0
    if running and not any(i["state"] == "running" for i in items):
        items.append({"n": auto["batch_number"], "state": "running", "count": auto.get("total", 0), "run_id": None,
                      "done": auto.get("done", 0), "stage": auto.get("stage", "")})
    last_n = max((i["n"] for i in items), default=0)
    limit = _batch_limit(t, pending) if pending else 0
    # 검사 중인 묶음의 파일은 아직 '대기' 목록에 남아 있다 — 고르기 전이면 한 묶음 크기만큼으로 본다
    # (진행 표시용 total은 단계마다 바뀌므로 쓰지 않는다. 검사 중 묶음은 최대 한 묶음 크기)
    in_flight = min(limit, pending) if running else 0
    waiting = max(0, -(-max(pending - in_flight, 0) // limit)) if limit else 0
    for k in range(1, waiting + 1):
        items.append({"n": last_n + k, "state": "waiting", "count": min(limit, pending - in_flight - (k - 1) * limit), "run_id": None})
    for i in items:
        if i["state"] == "running" and running:
            i.update(done=auto.get("done", 0), stage=auto.get("stage", ""), count=auto.get("total", 0) or i["count"])
    checked = [i for i in items if i["state"] != "waiting"]
    resumable = (not running) and pending > 0 and bool(checked) and checked[-1]["state"] == "done"
    return {"batches": items, "running": bool(running), "reason": auto.get("reason", "") if not running else "",
            "pending": pending, "done": sum(i["state"] == "done" for i in items), "total": len(items),
            "resumable": resumable}


@router.post("/s2/auto/pause")
def s2_auto_pause(request: Request):
    t = _t(request)
    job = getattr(t, "auto_job", None)
    if job and job.get("running"):
        job["stop"] = True
        t.flash = "지금 분류 중인 묶음을 마치면 멈춥니다."
    return RedirectResponse(_back(request), status_code=303)


@router.post("/s2/auto/resume")
def s2_auto_resume(request: Request):
    t = _t(request)
    roots, spec, redirect = _guard(request)
    if redirect:
        return redirect
    t.flash = "남은 묶음 분류를 이어서 시작했습니다." if _start_auto(t) else "이어서 분류할 묶음이 없습니다."
    return RedirectResponse(_back(request), status_code=303)


@router.get("/s2/series.json")
def s2_series_json(request: Request):
    t = _t(request)
    roots = t.db.list_roots()
    spec = SpecStore(t.db.conn).current()
    return Response(json.dumps(_series_bar(t, CatalogStore(t.db.conn), roots, spec), ensure_ascii=False),
                    media_type="application/json")


def _back(request: Request) -> str:
    """같은 화면(묶음·탭·쪽)으로 돌아간다 — referer의 경로만 사용."""
    ref = urlparse(request.headers.get("referer") or "")
    return (ref.path + ("?" + ref.query if ref.query else "")) if ref.path.startswith("/s2") else "/s2"


def _review_url(value: str, batch: int | None = None) -> str:
    """결과 화면으로 복귀하는 데 필요한 값만 다룬다."""
    try:
        ref = urlparse(value)
        q = parse_qs(ref.query, max_num_fields=32) if ref.path == "/s2" and not (ref.scheme or ref.netloc) else {}
    except ValueError:
        q = {}
    number = q.get("batch", [""])[0]
    if batch is not None and number != str(batch):
        q = {}
        number = str(batch)
    params = {"result": 1}
    if re.fullmatch(r"[1-9][0-9]{0,8}", number):
        params["batch"] = number
    if q.get("tab", [""])[0] in ("plan", "check", "keep"):
        params["tab"] = q["tab"][0]
    page = q.get("page", [""])[0]
    if re.fullmatch(r"[1-9][0-9]{0,8}", page):
        params["page"] = page
    return "/s2?" + urlencode(params)


def _start_prepare(t, roots, spec, previous: dict | None = None, *,
                   series_id: str | None = None, refresh: bool = False, resume_unapproved: bool = False,
                   filtered: tuple[list, Exclusions] | None = None) -> None:
    """한 서버에서 로컬 사전 검사 하나만 실행한다."""
    with t.setup_lock:
        if getattr(t, "prep_job", {}).get("running") or getattr(t, "job", {}).get("running") or _auto_running(t):
            return
        key = scope_key(roots, spec)
        previous_notice = json.loads(previous["notice"] or "{}") if previous else {}
        preferred_id = series_id or previous_notice.get("series_id")
        batch = previous_notice.get("batch_number", 0) + 1
        pjob = {"running": True, "stage": "폴더 탐색 준비", "done": 0, "total": 0, "batch_number": batch,
                "scope_key": key, "token": secrets.token_urlsafe(18), "started": time.time()}
        t.prep_job = pjob
        t.prepared = None
        scope = Scope(roots, t.home, t.app_data)

    def worker() -> None:
        try:
            catalog = CatalogStore(t.db.conn)
            inventory = InventoryStore(t.db.conn)
            series_id = (preferred_id or (inventory.unapproved(key) if resume_unapproved else None)
                         or secrets.token_hex(12))
            exists = inventory.exists(series_id, key)
            include_current = not spec["body"].get("collect_only")

            def stage(name, done, total):
                pjob.update(stage=name, done=done, total=total)

            if filtered is None and (refresh or not exists):
                # ① 파일 종류 살펴보기: DB에 저장하기 전에 확장자·몰린 폴더를 보여 주고 제외를 고르게 한다
                stage("파일 종류 살펴보기", 0, 0)
                pjob["frac"] = 0.0
                survey, items = run_survey(roots, scope, include_current, progress=lambda found: pjob.update(done=found),
                                           on_fraction=lambda f: pjob.update(frac=f))
                pjob["frac"] = 1.0
                t.survey = {"key": key, "survey": survey, "items": items, "series_id": series_id,
                            "previous": previous, "token": secrets.token_urlsafe(18)}
                pjob["awaiting_filter"] = True
                log.info("[파일종류] 발견 %d건, 확장자 %d종 — 제외 선택 대기", survey.total, len(survey.exts))
                return
            filtered_out = 0
            if filtered is not None:
                items, exclusions = filtered
                stage("목록 저장", 0, 0)
                legacy = catalog.series_progress(series_id) if not exists and previous else None
                saved = inventory.rebuild(series_id, key, roots, scope, include_current,
                                          progress=lambda found: pjob.update(done=found), legacy=legacy,
                                          items=items, keep=exclusions.keep, stage=stage)
                filtered_out = len(items) - saved
                log.info("[파일목록] 작업 %s 저장 완료 (제외 확장자 %d종·폴더 %d곳)",
                         series_id, len(exclusions.exts), len(exclusions.dirs))
            prep = _build_batch(t, roots, spec, scope, key, series_id, previous, batch, refresh, filtered_out,
                                stage, pjob)
            if previous and not prep.entries and catalog.latest()["run_id"] == previous["run_id"]:
                catalog.update_notice(previous["run_id"], previous_notice | {
                    k: prep.notice[k] for k in ("found", "truncated", "coverage_unknown", "scan_errors", "inventory_skipped")
                })
            t.prepared = prep
            log.info("[사전검사] 묶음 %d, 발견 %d건 · 이번 %d건 · 남은 %d건",
                     batch, prep.notice["found"], len(prep.entries), prep.notice["truncated"])
        except Exception:
            pjob["error"] = True
            log.exception("[사전검사] 묶음 준비 실패")
        finally:
            pjob["running"] = False

    threading.Thread(target=worker, daemon=True).start()


def _apply_filter_sync(request: Request, token: str | None, keep_exts: set[str], exclude_dirs: set[str]):
    t = _t(request)
    with t.setup_lock:
        roots, spec, redirect = _guard(request)
        if redirect:
            return redirect
        sv = getattr(t, "survey", None)
        pjob = getattr(t, "prep_job", {}) or {}
        if not sv or sv["key"] != scope_key(roots, spec) or token != sv["token"] \
                or not pjob.get("awaiting_filter"):
            t.flash = "파일 종류 목록이 바뀌었습니다. 다시 살펴봐 주세요."
            return RedirectResponse("/s2?new=1", status_code=303)
        known_exts = {e["ext"] for e in sv["survey"].exts}
        known_dirs = {h["key"] for h in sv["survey"].hotspots}
        ex = Exclusions({e for e in known_exts if e not in keep_exts},
                        {d for d in exclude_dirs if d in known_dirs})
        if not any(not item.is_dir and ex.keep(item) for item in sv["items"]):
            sv["rejected_filter"] = ex
            t.flash = "분류할 파일이 0개입니다. 파일 종류와 제외한 폴더를 다시 확인해 주세요."
            return RedirectResponse("/s2", status_code=303)
        FilterStore(t.db.conn).save(ex)
        pjob["awaiting_filter"] = False
        t.survey = None
        _start_prepare(t, roots, spec, previous=sv["previous"], series_id=sv["series_id"], refresh=True,
                       filtered=(sv["items"], ex))
    return RedirectResponse("/s2", status_code=303)


@router.post("/s2/filter")
async def s2_filter(request: Request):
    """제외할 확장자·폴더를 저장하고, 남은 항목만 목록(DB)에 저장한 뒤 검사를 이어 간다."""
    form = await request.form()
    token = form.get("token")
    keep_exts = set(form.getlist("keep_ext"))
    exclude_dirs = set(form.getlist("exclude_dir"))
    return await run_in_threadpool(_apply_filter_sync, request, token, keep_exts, exclude_dirs)


@router.post("/s2/check")
def s2_check(request: Request, run_id: str = Form(...), entry_id: list[str] = Form(default=[]), checked: str = Form("1"),
             action: str = Form(""), preview: str = Form(""), return_to: str = Form(""), folder: str = Form("")):
    """재배치 체크를 서버에 저장 — 결과를 쪽으로 나눠 보여 줘도 다른 쪽의 선택이 사라지지 않는다.
    체크박스 전체를 한꺼번에 보내지 않고 바뀐 것만 보낸다(폼 크기 제한 없음)."""
    t = _t(request)
    with t.setup_lock:
        store = CatalogStore(t.db.conn)
        roots = t.db.list_roots()
        spec = SpecStore(t.db.conn).current()
        run = series_run(store, roots, spec, run_id)
        if not run:
            return Response(status_code=409)
        entries = store.entries(run_id)
        movable = {e["entry_id"]: e for e in entries if e["verdict"] in CatalogStore.MOVABLE}
        ids = [i for i in entry_id if i in movable]
        message = ""
        if action == "approve_all":  # 승인 필요 전부 승인 → 정리 예정으로
            ids = [i for i, e in movable.items() if _needs_approval(e)]
            checked = "1"
            message = f"승인 필요 {len(ids)}개를 승인했습니다. 정리 예정에 들어갑니다."
        elif action == "plan_all":  # 정리 예정 전부 — 이동할 폴더가 겨루는(contested) 파일만 뺀다
            from tongdory2.web.progress import bucket as _b
            ids = [i for i, e in movable.items() if _b(e) == "plan" and not e.get("contested")]
            checked = "1"
            message = f"정리 예정 {len(ids)}개를 모두 선택했습니다."
        elif action == "safe_all":  # 안전한 추천(기본 체크 대상)만 모두 선택 — 이번 묶음 전체
            ids = [i for i, e in movable.items() if e.get("default_checked") and e["verdict"] in ("JOIN", "SPEC_NODE")]
            checked = "1"
        elif action in ("new_all", "new_none", "new_folder"):  # 새 폴더 승인 필요 — 전체·폴더별 한 번에
            ids = [i for i, e in movable.items() if e["verdict"] == "NEW_NODE" and not _assigned(e)
                   and (action != "new_folder" or e.get("node_path") == folder)]
            checked = "0" if action == "new_none" else "1"
            message = (f"새 폴더 승인 필요 {len(ids)}개의 선택을 해제했습니다." if action == "new_none"
                       else f"새 폴더 {'‘' + folder + '’ ' if action == 'new_folder' else ''}승인 필요 {len(ids)}개를 선택했습니다.")
        elif action in ("dups_add", "dups_clear"):
            dup_ids = {i for i, e in movable.items() if e["verdict"] == "DUP"}
            ids = [i for i in ids if i in dup_ids] if action == "dups_add" else list(dup_ids)
            checked = "1" if action == "dups_add" else "0"
            message = (f"사본 {len(ids)}개를 휴지통 후보로 담았습니다. 실제 파일은 지우지 않았습니다."
                       if action == "dups_add" else "휴지통 후보 목록을 비웠습니다.")
        elif action == "leave_check":
            new_folders = _new_folders(roots, entries, store.effective_selection(run_id, entries))
            ids = [i for folder_ids in new_folders.values() for i in folder_ids]
            checked = "0"
            message = (f"이번 묶음에서 {len(ids)}개 파일의 이동 선택을 해제했습니다. 파일은 원래 위치에 둡니다."
                       if ids else "이번 묶음에 선택된 새 폴더 대상이 없습니다.")
            log.info("[일괄선택] 묶음 %s · 새 이동할 폴더 %d곳 · 이동 선택 해제 %d건", run_id, len(new_folders), len(ids))
        store.set_checked(run_id, ids, checked == "1")
        if checked == "1":  # 체크 = 승인: 승인 필요였던 파일은 정리 예정으로 옮긴다
            store.approve(run_id, [i for i in ids if _needs_approval(movable[i])])
        selected = store.effective_selection(run_id, entries)
    if action:
        t.flash = message or f"선택을 바꿨습니다 · 지금 {len(selected)}개 선택"
        back = _review_url(return_to, notice_of(run).get("batch_number", 1)) if return_to else _back(request)
        return RedirectResponse(back, status_code=303)
    total = count_selected(store, [r["run_id"] for r in done_runs(store, roots, spec)])
    result = {"checked": len(selected), "checked_all": total}
    if preview == "1":
        from tongdory2.web.progress import state

        new_folders = _new_folders(roots, entries, selected)
        paths = {p for i in ids if (p := movable[i].get("node_path"))}
        affected = {str(parent) for p in paths for parent in (Path(p), *Path(p).parents)}
        tree = state(t, tree_paths=affected)["tree"] if affected else []
        group_keys = {movable[i].get("group_key") for i in ids} - {None, ""}
        result.update(new_folders=len(new_folders), new_files=sum(map(len, new_folders.values())),
                      tree_counts={n["path"]: n["n"] for n in tree},
                      group_counts={c["key"]: c["checked"] for c in _group_cards(entries, selected, keys=group_keys)}
                      if group_keys else {})
    return Response(json.dumps(result), media_type="application/json")


def _new_folders(roots, entries: list[dict], selected: set[str]) -> dict[Path, list[str]]:
    current = next((r for r in roots if r.role == Role.CURRENT and r.confirmed), None)
    return missing_destination_folders(entries, selected, current.path) if current else {}


def _dup_rows(entries: list[dict], checked: set[str], home) -> list[dict]:
    """F07 내용이 똑같은 사본과 남겨둘 원본을 나란히 보여 준다(체크하면 휴지통 후보 — 실행 전까지 지우지 않음)."""
    by_id = {e["entry_id"]: e for e in entries}
    rows = []
    for e in entries:
        if e["verdict"] != "DUP":
            continue
        orig = by_id.get(e.get("dup_of") or "")
        rows.append({"id": e["entry_id"], "dup_path": tilde(Path(e["path"]), home),
                     "orig": tilde(Path(orig["path"]), home) if orig else "(이전 묶음의 원본)",
                     "size": e.get("size") or 0, "checked": e["entry_id"] in checked})
    return rows


def _group_cards(entries: list[dict], checked: set[str], *, keys: set[str] | None = None) -> list[dict]:
    """#47 묶음별 요약 카드 (쪽 나눔과 상관없이 이번 묶음 전체)."""
    cards: dict[str, dict] = {}
    for e in entries:
        if e["verdict"] not in ("GROUP", "ETC") or not (key := e.get("group_key")) or (keys is not None and key not in keys):
            continue
        c = cards.setdefault(key, {"key": key, "label": e.get("group_label") or "",
                                   "kind": e.get("group_kind") or "", "path": e.get("node_path") or "",
                                   "count": 0, "checked": 0, "examples": []})
        c["count"] += 1
        c["checked"] += e["entry_id"] in checked
        if len(c["examples"]) < 3:
            c["examples"].append(e["name"])
    order = {"folder": 0, "name": 1, "etc": 2}
    return sorted(cards.values(), key=lambda c: (order.get(c["kind"], 3), -c["count"], c["path"]))


@router.post("/s2/group")
def s2_group(request: Request, run_id: str = Form(...), group_key: str = Form(...), checked: str = Form("1")):
    """묶음 하나를 한 번에 체크/해제 (#47 — 파일마다 체크하지 않게)."""
    t = _t(request)
    with t.setup_lock:
        store = CatalogStore(t.db.conn)
        roots = t.db.list_roots()
        if not series_run(store, roots, SpecStore(t.db.conn).current(), run_id):
            t.flash = "분석 결과가 바뀌었습니다. 현재 결과를 다시 확인해 주세요."
            return RedirectResponse("/s2", status_code=303)
        entries = store.entries(run_id)
        ids = [e["entry_id"] for e in entries if e.get("group_key") == group_key and e["verdict"] in ("GROUP", "ETC")
               and e.get("sensitivity") != "L3"]
        store.set_checked(run_id, ids, checked == "1")
    path = next((e["node_path"] for e in entries if e.get("group_key") == group_key), "")
    t.flash = (f"'{path}' 묶음 {len(ids)}개를 정리 계획에 넣었습니다." if checked == "1"
               else f"'{path}' 묶음 {len(ids)}개를 원래 위치에 둡니다.")
    return RedirectResponse(_back(request), status_code=303)


@router.post("/s2/rescan")
def s2_rescan(request: Request):
    t = _t(request)
    with t.setup_lock:
        roots, spec, redirect = _guard(request)
        if redirect:
            return redirect
        if _auto_running(t):
            t.flash = "남은 묶음을 분류하는 중입니다. [멈춤]을 누르고 멈춘 뒤 다시 분석해 주세요."
            return RedirectResponse("/s2", status_code=303)
        _start_prepare(t, roots, spec)
    return RedirectResponse("/s2", status_code=303)


@router.post("/s2/retry")
def s2_retry(request: Request, run_id: str = Form(...)):
    t = _t(request)
    with t.setup_lock:
        roots, spec, redirect = _guard(request)
        if redirect:
            return redirect
        if _auto_running(t):
            t.flash = "남은 묶음을 분류하는 중입니다. [멈춤]을 누르고 멈춘 뒤 다시 해 주세요."
            return RedirectResponse("/s2", status_code=303)
        catalog = CatalogStore(t.db.conn)
        latest = catalog.latest()
        if not latest or latest["run_id"] != run_id or latest["status"] not in ("FAILED", "RUNNING"):
            return RedirectResponse("/s2", status_code=303)
        notice = json.loads(latest["notice"] or "{}")
        if notice.get("scope_key") != scope_key(roots, spec):
            return RedirectResponse("/s2", status_code=303)
        previous = catalog.get(notice["after_run"]) if notice.get("after_run") else None
        _start_prepare(t, roots, spec, previous=previous, series_id=notice.get("series_id"))
    return RedirectResponse("/s2", status_code=303)


@router.post("/s2/refresh")
def s2_refresh(request: Request, run_id: str = Form(...)):
    t = _t(request)
    with t.setup_lock:
        roots, spec, redirect = _guard(request)
        if redirect:
            return redirect
        if _auto_running(t):
            t.flash = "남은 묶음을 분류하는 중입니다. [멈춤]을 누르고 멈춘 뒤 다시 해 주세요."
            return RedirectResponse("/s2", status_code=303)
        done = done_runs(CatalogStore(t.db.conn), roots, spec)
        run = done[-1] if done else None
        if not run or run["run_id"] != run_id:
            t.flash = "가장 최근 묶음 화면에서 파일 목록을 다시 갱신해 주세요."
            return RedirectResponse("/s2", status_code=303)
        _start_prepare(t, roots, spec, previous=run, refresh=True)
    return RedirectResponse("/s2", status_code=303)


@router.post("/s2/next")
def s2_next(request: Request, run_id: str = Form(...)):
    t = _t(request)
    with t.setup_lock:
        roots, spec, redirect = _guard(request)
        if redirect:
            return redirect
        if _auto_running(t):
            return RedirectResponse("/s2", status_code=303)
        done = done_runs(CatalogStore(t.db.conn), roots, spec)
        run = done[-1] if done else None
        if not run or run["run_id"] != run_id:
            t.flash = "이미 다른 묶음이 열려 있습니다. 현재 결과를 확인해 주세요."
            return RedirectResponse("/s2", status_code=303)
        prep = getattr(t, "prepared", None)
        if prep and prep.notice.get("after_run") == run_id:
            return RedirectResponse("/s2", status_code=303)
        if not json.loads(run["notice"]).get("truncated"):
            t.flash = "이 분석에 남은 묶음이 없습니다."
            return RedirectResponse("/s2", status_code=303)
        if AUTO_CONTINUE:
            _start_auto(t)
        else:
            _start_prepare(t, roots, spec, previous=run)
    return RedirectResponse("/s2", status_code=303)


@router.post("/s2/analyze")
def s2_analyze(request: Request, prep_token: str = Form("")):
    t = _t(request)
    with t.setup_lock:
        roots, spec, redirect = _guard(request)
        if redirect:
            return redirect
        prep = getattr(t, "prepared", None)
        pjob = getattr(t, "prep_job", {})
        if getattr(t, "job", {}).get("running"):
            return RedirectResponse("/s2", status_code=303)
        if not prep:
            t.flash = "이 전송 확인은 이미 처리됐습니다. 현재 분석 상태를 확인해 주세요."
            return RedirectResponse("/s2", status_code=303)
        if prep.notice.get("scope_key") != scope_key(roots, spec):
            t.flash = "정리 설정이 바뀌었습니다. 전송 내역을 다시 확인해 주세요."
            return RedirectResponse("/s2?new=1", status_code=303)
        if prep_token != pjob.get("token"):
            t.flash = "전송 내역이 바뀌었습니다. 현재 내역을 다시 확인해 주세요."
            return RedirectResponse("/s2", status_code=303)
        t.prepared = None
        store = CatalogStore(t.db.conn)
        run_id = store.start(spec["version"], prep.notice)
        job = {"running": True, "local": True, "run_id": run_id, "stage": "준비", "done": 0, "total": 0,
               "batch_number": prep.notice["batch_number"]}
        t.job = job
    db_path = t.settings.db_path
    scope = Scope(roots, t.home, t.app_data)

    def worker() -> None:
        conn = None
        err = "ERROR"
        try:
            conn = sqlite3.connect(str(db_path), timeout=10)
            err = _save_batch(conn, run_id, prep, scope, job)
        except Exception:
            log.exception("[목록화] 결과 저장 실패")
        finally:
            if conn:
                conn.close()
            job["running"] = False
        if err is None and AUTO_CONTINUE and prep.notice.get("truncated"):
            _start_auto(t)  # 남은 묶음은 뒤에서 이어서 로컬 검사 — 사용자는 완료된 묶음부터 검토

    threading.Thread(target=worker, daemon=True).start()
    return RedirectResponse("/s2", status_code=303)


async def _assignment_selection(request: Request) -> tuple[dict[str, str], list[str]]:
    return await read_selection(request, "entry_ids", "run_id", "target")


async def _document_selection(request: Request) -> tuple[dict[str, str], list[str]]:
    return await read_selection(request, "entry_ids", "run_id", optional=("return_to",))

async def _document_approval(request: Request) -> tuple[dict[str, str], list[str]]:
    fields, selected = await read_selection(request, "entry_ids", "run_id", "approval", "reviewed", optional=("return_to",))
    if fields["reviewed"] != "yes":
        raise HTTPException(422, "전송 내용을 확인해 주세요.")
    return fields, selected


def _standard_query(identifier: str) -> str | None:
    number = identifier.strip().upper()
    patterns = (("RFC", r"RFC[ -]?([0-9]{3,5})"), ("SGP.", r"(?:GSMA )?SGP[. -]?([0-9]{2})"),
                ("ISO", r"ISO[ -]?([0-9]{3,6}(?:-[0-9]{1,3})?)"),
                ("3GPP TS", r"(?:3GPP )?TS ?([0-9]{2}\.[0-9]{3})"))
    for prefix, pattern in patterns:
        if match := re.fullmatch(pattern, number):
            sep = "" if prefix.endswith(".") else " "
            return f"{prefix}{sep}{match[1]} official specification"
    return None


def _standards_page(request, identifier="", query=None, results=None, error="", *, return_to=""):
    t = _t(request)
    back_url = "/"
    if return_to != "/":
        roots, spec = t.db.list_roots(), SpecStore(t.db.conn).current()
        if spec and view_run(CatalogStore(t.db.conn), roots, spec, None):
            back_url = _review_url(return_to) if return_to else "/s2/back"
    return templates.TemplateResponse(request, "s2_standards.html", {
        "identifier": identifier, "query": query, "results": results, "error": error,
        "has_key": bool(t.settings.liner_api_key),
        "back_url": back_url, "return_to": back_url if return_to else "",
        "approval": approval_token(t, "standard-search", query) if query else "",
    }, status_code=422 if error else 200)


@router.get("/s2/standards", response_class=HTMLResponse)
def standards_preview(request: Request, identifier: str = "", return_to: str = ""):
    if not identifier:
        return _standards_page(request, return_to=return_to)
    query = _standard_query(identifier)
    return _standards_page(request, identifier, query,
                           error="" if query else "RFC 8446처럼 공개 규격 번호만 입력해 주세요.", return_to=return_to)


@router.post("/s2/standards", response_class=HTMLResponse)
def standards_search(request: Request, identifier: str = Form(...), approval: str = Form(...), return_to: str = Form("")):
    from tongdory2.liner.client import LinerClient, LinerError

    t = _t(request)
    query = _standard_query(identifier)
    if not query or not approved(t, approval, "standard-search", query):
        return _standards_page(request, identifier, query, error="검색어를 미리 확인한 뒤 다시 승인해 주세요.", return_to=return_to)
    if not t.settings.liner_api_key:
        return _standards_page(request, identifier, query, error="검색하려면 Liner API 키를 설정해 주세요.", return_to=return_to)

    async def go():
        async with LinerClient(t.settings.liner_api_key, t.settings.liner_base_url,
                               conn=t.db.conn, run_id="standard-search") as client:
            return await client.search_web(query)

    try:
        refs = asyncio.run(go())
        results = [r for r in refs if isinstance(r, dict) and isinstance(r.get("url"), str)
                   and re.match(r"https?://[^/\s]+", r["url"])]
        log.info("[규격검색] 승인된 공개 규격 검색 완료 · 결과 %d건", len(results))
    except LinerError as error:
        log.warning("[규격검색] 승인된 검색 실패 (%s)", error.code)
        return _standards_page(request, identifier, query, error="검색을 마치지 못했습니다. API 설정을 확인해 주세요.", return_to=return_to)
    return _standards_page(request, identifier, query, results, return_to=return_to)


def _document_review(request, selection, *, excluded: list[dict] | None = None):
    values, ids = selection
    t = _t(request)
    result = RedirectResponse("/s2?result=1&tab=check", status_code=303)
    with t.setup_lock:
        roots, spec, redirect = _guard(request)
        if redirect:
            return redirect
        if getattr(t, "job", {}).get("running") or getattr(t, "prep_job", {}).get("running"):
            return result
        store = CatalogStore(t.db.conn)
        run = series_run(store, roots, spec, values["run_id"])
        if not run:
            t.flash = "현재 확인 목록에서 문서를 다시 선택해 주세요."
            return result
        result = RedirectResponse(_document_origin(request, values, run), status_code=303)
        notice = json.loads(run["notice"])
        known = {e["entry_id"]: e for e in store.entries(run["run_id"])}
        selected = list(dict.fromkeys(ids))
        if not selected or any(entry_id not in known for entry_id in selected) or not notice.get("series_id"):
            t.flash = "확인 목록에서 문서 분석이 가능한 파일을 선택해 주세요."
            return result
        candidates = {eid: known[eid] for eid in selected if document_candidate(known[eid])}
        if not candidates and excluded is None:
            t.flash = "선택한 파일 중 본문 분석이 가능한 문서가 없습니다. 파일명 분석 또는 직접 지정을 이용해 주세요."
            return result
        if excluded is not None:
            for eid in selected:
                if eid in candidates:
                    continue
                entry = known[eid]
                if entry.get("sensitivity") == "L3":
                    reason = "민감 파일은 본문 분석 대상에서 제외합니다."
                elif entry.get("override"):
                    reason = "이미 이동할 폴더를 직접 지정한 파일입니다."
                elif entry.get("kind") in (None, "image", "binary"):
                    reason = "본문 텍스트 추출을 지원하지 않는 파일입니다. 파일명 분석 또는 직접 지정을 이용해 주세요."
                else:
                    reason = "현재 확인 필요 문서가 아닙니다. 분류 결과를 다시 확인해 주세요."
                excluded.append({"entry_id": eid, "name": entry["name"], "verdict": "HOLD", "reason": reason})
        if not t.settings.liner_api_key:
            t.flash = "문서 분석을 요청하려면 Liner API 키를 설정해 주세요."
            return result
        scope = Scope(roots, t.home, t.app_data)
        entry_fields = {field.name for field in fields(Entry)}
        entries = [Entry(**{k: v for k, v in candidates[entry_id].items() if k in entry_fields})
                   for entry_id in selected if entry_id in candidates]
        return run, notice, roots, spec, scope, entries


def _document_origin(request: Request, values: dict, run: dict) -> str:
    batch = notice_of(run).get("batch_number", 1)
    url = _review_url(values.get("return_to") or _back(request), batch)
    return url if "&tab=" in url else f"{url}&tab=check"


@router.post("/s2/documents/preview", response_class=HTMLResponse)
def s2_document_preview(request: Request, selection: tuple[dict[str, str], list[str]] = Depends(_document_selection)):
    excluded = []
    review = _document_review(request, selection, excluded=excluded)
    if isinstance(review, RedirectResponse):
        return review
    run, notice, roots, spec, scope, entries = review
    t = _t(request)
    _stage_documents(t.db.conn, notice["series_id"], scope, entries, t.settings.recent_hours)
    ready = [entry for entry in entries if entry.verdict == "PENDING"]
    current = next(r for r in roots if r.role == Role.CURRENT and r.confirmed)
    projects = [current.path.name]
    if not spec["body"].get("collect_only"):
        projects.extend(r.path.name for r in roots if r.role == Role.BASELINE)
    return templates.TemplateResponse(request, "s2_document_preview.html", {
        "step": "s2", "mode": "documents", "submit_action": "/s2/documents",
        "run_id": run["run_id"], "ready": ready, "entries": entries + excluded,
        "approval": approval_token(t, "document-review", run["run_id"], _document_payload(entries)),
        "return_to": _document_origin(request, selection[0], run),
        "projects": projects, "folders": [document_folder_text(n) for n in document_folders(spec)],
    }, headers={"Cache-Control": "no-store"})


def _name_review(request, selection):
    values, ids = selection
    t = _t(request)
    result = RedirectResponse("/s2?result=1&tab=check", status_code=303)
    with t.setup_lock:
        roots, spec, redirect = _guard(request)
        if redirect:
            return redirect
        if getattr(t, "job", {}).get("running") or getattr(t, "prep_job", {}).get("running"):
            return result
        store = CatalogStore(t.db.conn)
        run = series_run(store, roots, spec, values["run_id"])
        if not run:
            t.flash = "현재 확인 목록에서 파일을 다시 선택해 주세요."
            return result
        result = RedirectResponse(_document_origin(request, values, run), status_code=303)
        notice = json.loads(run["notice"])
        all_by_id = {e["entry_id"]: e for e in store.entries(run["run_id"])}
        selected = [eid for eid in dict.fromkeys(ids) if eid in all_by_id]
        if not selected or not notice.get("series_id"):
            t.flash = "확인 목록에서 분석할 파일을 선택해 주세요."
            return result
        if not t.settings.liner_api_key:
            t.flash = "파일명 분석을 요청하려면 Liner API 키를 설정해 주세요."
            return result
        scope = Scope(roots, t.home, t.app_data)
        entry_fields = {field.name for field in fields(Entry)}
        entries = []
        for eid in selected:
            row = all_by_id[eid]
            entry = Entry(**{k: v for k, v in row.items() if k in entry_fields})
            if row.get("override"):
                entry.override = row["override"]
            entries.append(entry)
        return run, notice, roots, spec, scope, entries


def _stage_names(entries: list[Entry]) -> list[Entry]:
    from tongdory2.fsx import sensitivity as fx_sens

    ready: list[Entry] = []
    for entry in entries:
        if entry.sensitivity == "L3":
            entry.payload = ""
            entry.reason = "민감 파일은 파일명도 전송하지 않습니다."
            if entry.verdict == "PENDING":
                entry.verdict, entry.reason_code = "HOLD", "SENSITIVE_EXCLUDED"
            continue
        if not name_candidate({"verdict": entry.verdict, "override": getattr(entry, "override", None),
                               "sensitivity": entry.sensitivity}):
            entry.payload = ""
            entry.reason = entry.reason or "파일명 분석 대상이 아닙니다."
            continue
        entry.payload = f"파일명: {name_payload(entry)}"
        entry.verdict, entry.reason_code = "PENDING", None
        ready.append(entry)
    return ready


@router.post("/s2/names/preview", response_class=HTMLResponse)
def s2_names_preview(request: Request, selection: tuple[dict[str, str], list[str]] = Depends(_document_selection)):
    review = _name_review(request, selection)
    if isinstance(review, RedirectResponse):
        return review
    run, notice, roots, spec, scope, entries = review
    t = _t(request)
    ready = _stage_names(entries)
    current = next(r for r in roots if r.role == Role.CURRENT and r.confirmed)
    return templates.TemplateResponse(request, "s2_document_preview.html", {
        "step": "s2", "mode": "names", "submit_action": "/s2/names",
        "run_id": run["run_id"], "ready": ready, "entries": entries,
        "approval": approval_token(t, "name-review", run["run_id"], _document_payload(entries)),
        "return_to": _document_origin(request, selection[0], run),
        "projects": [current.path.name],
        "folders": [document_folder_text(n) for n in document_folders(spec)],
    }, headers={"Cache-Control": "no-store"})


@router.post("/s2/names")
def s2_names(request: Request, selection: tuple[dict[str, str], list[str]] = Depends(_document_approval)):
    t = _t(request)
    with t.setup_lock:
        review = _name_review(request, selection)
        if isinstance(review, RedirectResponse):
            return review
        run, notice, roots, spec, scope, entries = review
        job = {"running": True, "stage": "파일명 기반 Liner 분석", "done": 0, "total": len(entries),
               "batch_number": notice.get("batch_number"), "document": True, "ticket": secrets.token_urlsafe(16),
               "return_to": _document_origin(request, selection[0], run)}
        t.job = job
        thread = threading.Thread(target=_analyze_names, args=(t, *review, job, selection[0]["approval"]),
                                  daemon=True)
        thread.start()
    return RedirectResponse(f"{job['return_to']}&document_job={job['ticket']}", status_code=303)


def _analyze_names(t, run, notice, roots, spec, scope, entries, job, approval) -> None:
    from tongdory2.liner.client import LinerClient

    run_id = run["run_id"]
    settings = t.settings
    conn = None
    cost = 0.0
    try:
        conn = sqlite3.connect(str(settings.db_path), timeout=10)
        ready = _stage_names(entries)
        if not approved(t, approval, "name-review", run_id, _document_payload(entries)):
            job["message"] = "전송 내용이 바뀌었거나 승인되지 않았습니다. 미리보기에서 다시 확인해 주세요."
            log.info("[파일명분석] 전송 미리보기와 일치하지 않아 요청 차단")
            return
        current = next(r for r in roots if r.role == Role.CURRENT and r.confirmed)
        if ready:
            with t.setup_lock:
                if not current_run(run, t.db.list_roots(), SpecStore(t.db.conn).current()):
                    log.info("[파일명분석] 승인 후 정리 설정 변경 · 전송 취소")
                    return

            async def go():
                nonlocal cost
                async with LinerClient(settings.liner_api_key, settings.liner_base_url, conn=conn, run_id=run_id) as client:
                    try:
                        await classify_by_names(ready, spec, current.path, client, job)
                    finally:
                        cost = client.total_cost_usd

            try:
                asyncio.run(go())
            except Exception:
                log.exception("[파일명분석] 파일명 Liner 분석 중 오류")
                for entry in ready:
                    if entry.verdict == "PENDING":
                        entry.verdict, entry.reason_code = "HOLD", "NOT_ANALYZED"
                        entry.reason = "파일명 분석을 마치지 못했습니다. 다시 요청하거나 직접 지정해 주세요."

        store = CatalogStore(conn)
        bodies = entries_to_dicts(entries)
        updates = {e["entry_id"]: e for e in bodies}
        previous_entries = store.entries(run_id)
        current_entries = [e | updates.get(e["entry_id"], {}) for e in previous_entries]
        failed = sum(e.get("reason_code") == "NOT_ANALYZED" for e in current_entries)
        from tongdory2.web.progress import bucket

        plan_before = sum(bucket(e) == "plan" for e in previous_entries)
        plan_after = sum(bucket(e) == "plan" for e in current_entries)
        notice.update(send_names=notice.get("send_names", 0) + len(ready),
                      analysis_errors=failed, total_errors=notice.get("prior_errors", 0) + failed)
        store.apply_document_results(
            run_id, bodies, cost, notice,
            analyzed=Prepared(ready, {}, current, spec),
            plan_before=plan_before, plan_after=plan_after,
        )
        log.info("[파일명분석] 선택 %d건 · 분석 대상 %d건 · 오류 %d건", len(entries), len(ready), failed)
        gained = plan_after - plan_before
        matched = sum(e.reason_code == "LINER_NAME_MATCH" for e in ready)
        new_folders = sum(e.reason_code == "LINER_NAME_NEW" for e in ready)
        missed = len(ready) - matched - new_folders
        job["message"] = (f"파일명 Liner 분석 완료 · {len(ready)}개 중 {matched + new_folders}개 폴더를 찾았습니다 — "
                          f"기존 폴더 {matched}개 · 새 폴더 추천 {new_folders}개 · 못 찾음 {missed}개(직접 확인에 남김)")
        # 팝업에 파일별 결과를 보인다 (찾은 것 먼저, 최대 50개)
        outcome = {"LINER_NAME_MATCH": "기존 폴더", "LINER_NAME_NEW": "새 폴더"}
        rows = [{"name": e.name, "kind": outcome.get(e.reason_code, "못 찾음"),
                 "path": e.node_path if e.reason_code in outcome else "",
                 "reason": reason_brief(e.reason)} for e in ready]
        job["result_rows"] = sorted(rows, key=lambda r: r["kind"] == "못 찾음")[:50]
        if failed:
            job["message"] += f" · 오류 {failed}개는 다시 요청하거나 직접 지정하세요."
        job["suggested"] = gained > 0
    except Exception:
        log.exception("[파일명분석] 결과 저장 실패")
    finally:
        if conn is not None:
            conn.close()
        job["running"] = False


@router.post("/s2/documents")
def s2_documents(request: Request, selection: tuple[dict[str, str], list[str]] = Depends(_document_approval)):
    t = _t(request)
    with t.setup_lock:
        review = _document_review(request, selection)
        if isinstance(review, RedirectResponse):
            return review
        run, notice, roots, spec, scope, entries = review
        job = {"running": True, "stage": "선택한 문서 확인", "done": 0, "total": len(entries),
               "batch_number": notice.get("batch_number"), "document": True, "ticket": secrets.token_urlsafe(16),
               "return_to": _document_origin(request, selection[0], run)}
        t.job = job
        thread = threading.Thread(target=_analyze_documents, args=(t, *review, job, selection[0]["approval"]),
                                  daemon=True)
        thread.start()
    return RedirectResponse(f"{job['return_to']}&document_job={job['ticket']}", status_code=303)


def _document_payload(entries: list[Entry]) -> str:
    return json.dumps([(entry.entry_id, entry.verdict, entry.reason_code, entry.payload) for entry in entries])


def _stage_documents(conn, series_id, scope, entries, recent_hours, job=None):
    check = InventoryCheck(scope)
    inventory = {}
    excerpts = 0
    for i, entry in enumerate(entries, 1):
        item = completed_item(conn, series_id, entry.path)
        if job is not None:
            job["done"] = i
        if item and item.mtime_ns == entry.mtime_ns and item.size == entry.size and check.current(item) == item:
            excerpt = load_document(entry, item, scope, recent_hours)
            if check.current(item) == item:
                if excerpt is not None:
                    inventory[entry.entry_id] = item
                    excerpts += int(excerpt)
                continue
        entry.verdict, entry.reason_code, entry.payload = "HOLD", "DOCUMENT_CHANGED", ""
        entry.reason = "파일이 변경됐습니다. 새 파일·변경 파일 확인을 실행해 주세요."
    return inventory, excerpts


def _analyze_documents(t, run, notice, roots, spec, scope, entries, job, approval) -> None:
    from tongdory2.liner.client import LinerClient

    run_id = run["run_id"]
    settings = t.settings
    conn = None
    cost = 0.0
    try:
        conn = sqlite3.connect(str(settings.db_path), timeout=10)
        inventory, excerpts = _stage_documents(conn, notice["series_id"], scope, entries, settings.recent_hours, job)
        if not approved(t, approval, "document-review", run_id, _document_payload(entries)):
            job["message"] = "전송 내용이 바뀌었거나 승인되지 않았습니다. 미리보기에서 다시 확인해 주세요."
            log.info("[문서분석] 전송 미리보기와 일치하지 않아 요청 차단")
            return
        ready = [entry for entry in entries if entry.entry_id in inventory]
        current = next(r for r in roots if r.role == Role.CURRENT and r.confirmed)
        analyzed = Prepared(ready, {}, current, spec)

        if ready:
            with t.setup_lock:
                if not current_run(run, t.db.list_roots(), SpecStore(t.db.conn).current()):
                    log.info("[문서분석] 승인 후 정리 설정 변경 · 전송 취소")
                    return
            baselines = [r.path.name for r in roots if r.role == Role.BASELINE] if not spec["body"].get("collect_only") else []

            async def go():
                nonlocal cost
                async with LinerClient(settings.liner_api_key, settings.liner_base_url, conn=conn, run_id=run_id) as client:
                    try:
                        await analyze(analyzed, client, baselines, job)
                    finally:
                        cost = client.total_cost_usd

            try:
                asyncio.run(go())
            except Exception:
                log.exception("[문서분석] 선택한 문서 분석 중 오류")
                for entry in ready:
                    if entry.verdict == "PENDING":
                        entry.verdict, entry.reason_code = "HOLD", "NOT_ANALYZED"
                        entry.reason = "문서 분석을 마치지 못했습니다. 다시 요청하거나 직접 지정해 주세요."

        for entry in ready:  # 4단계에서도 못 정한 낱개는 다시 '기타' 후보로 (#47)
            if entry.group_kind == "etc" and entry.verdict == "HOLD":
                entry.verdict, entry.reason_code = "ETC", "GROUP_ETC"
                entry.node_path, entry.new_node_name = entry.node_path or "자동분류/기타", "기타"
                entry.reason = "Liner도 분류 희망 폴더 목록에서 맞는 곳을 찾지 못함 — 기타 폴더로 보내거나 직접 지정"
                entry.default_checked = False
            elif entry.group_kind == "etc" and entry.verdict == "NEW_NODE" and not entry.node_path:
                entry.verdict = "ETC"
        store = CatalogStore(conn)
        bodies = entries_to_dicts(entries)
        updates = {e["entry_id"]: e for e in bodies}
        previous_entries = store.entries(run_id)
        current_entries = [e | updates.get(e["entry_id"], {}) for e in previous_entries]
        failed = sum(e.get("reason_code") == "NOT_ANALYZED" for e in current_entries)
        from tongdory2.web.progress import bucket

        plan_before = sum(bucket(e) == "plan" for e in previous_entries)
        plan_after = sum(bucket(e) == "plan" for e in current_entries)
        notice.update(send_names=notice.get("send_names", 0) + len(ready),
                      send_excerpts=notice.get("send_excerpts", 0) + excerpts, analysis_errors=failed,
                      total_errors=notice.get("prior_errors", 0) + failed)
        store.apply_document_results(run_id, bodies, cost, notice, analyzed=analyzed, inventory=inventory,
                                     plan_before=plan_before, plan_after=plan_after)
        log.info("[문서분석] 선택 %d건 · 분석 대상 %d건 · 오류 %d건", len(entries), len(ready), failed)
        gained = plan_after - plan_before
        etc_left = sum(e.verdict == "ETC" for e in ready)
        # #41 다음 단계 안내
        job["message"] = (f"Liner 분석 완료 · {len(ready)}개 중 {gained}개의 이동할 폴더를 제안했습니다. "
                          "제안받은 파일은 [정리 예정]에 들어갔습니다."
                          if gained > 0 else f"Liner 분석 완료 · {len(ready)}개 모두 분류 희망 폴더 목록에서 맞는 곳을 찾지 못했습니다.")
        if etc_left:
            job["message"] += f" 못 정한 {etc_left}개는 '기타' 묶음에 남겼습니다 — [기타 폴더 이동 대상으로 선택]를 누르면 계획에 들어갑니다."
        if failed:
            job["message"] += f" 오류 {failed}개는 다시 요청하거나 직접 지정하세요."
        job["suggested"] = gained > 0
    except Exception:
        log.exception("[문서분석] 결과 저장 실패")
    finally:
        if conn is not None:
            conn.close()
        job["running"] = False


@router.post("/s2/assign")
def s2_assign(request: Request, selection: tuple[dict[str, str], list[str]] = Depends(_assignment_selection)):
    fields, entry_ids = selection
    run_id, target = fields["run_id"], fields["target"]
    t = _t(request)
    with t.setup_lock:
        roots, spec, redirect = _guard(request)
        if redirect:
            return redirect
        if getattr(t, "job", {}).get("running"):
            return RedirectResponse("/s2", status_code=303)
        valid_nodes = {n["node_id"] for n in spec["body"]["nodes"] if n["status"] == "ADOPTED" and n["role"] == "DEST"}
        if target not in valid_nodes | {"HOLD", "TRASH", "CLEAR", "LEAVE"}:
            raise HTTPException(400, "알 수 없는 지정 대상")
        store = CatalogStore(t.db.conn)
        run = series_run(store, roots, spec, run_id)
        if not run:
            t.flash = "분석 결과가 바뀌었습니다. 현재 결과를 다시 확인해 주세요."
            return RedirectResponse("/s2", status_code=303)
        known = {e["entry_id"] for e in store.entries(run_id) if e["verdict"] != "EXCLUDE"}
        ids = [i for i in dict.fromkeys(entry_ids) if i in known]
        store.set_override(run_id, ids, target)
        if target == "LEAVE":
            t.flash = f"{len(ids)}개 파일을 원래 위치에 둡니다."
        elif target == "CLEAR":
            t.flash = "직접 지정과 이동 선택을 해제했습니다."
        else:
            t.flash = f"{len(ids)}개 파일을 직접 지정했습니다."
    back = _back(request)
    anchor = "#shots" if request.query_params.get("to") == "shots" else ""
    return RedirectResponse(("/s2" if back == "/s2" else back) + anchor, status_code=303)


# ----------------------------------------------------------------- 로컬 뷰어 (외부 전송 0)

def _entry_file(request: Request, run_id: str, entry_id: str) -> tuple[dict, Path]:
    t = _t(request)
    e = CatalogStore(t.db.conn).entry(run_id, entry_id)
    if not e:
        raise HTTPException(404)
    p = Path(e["path"])
    if not t.scope().check(p, Access.READ).allowed or not p.is_file():
        raise HTTPException(403, "범위 밖")
    return e, p


@router.get("/s2/thumb/{run_id}/{entry_id}")
def s2_thumb(request: Request, run_id: str, entry_id: str, reveal: int = 0):
    from PIL import Image, ImageFilter

    e, p = _entry_file(request, run_id, entry_id)
    blur = e.get("sensitivity") == "L3" and not reveal
    t = _t(request)
    cache_dir = t.settings.data_dir / "thumbs"  # 사용자 폴더가 아니라 앱 데이터 폴더
    headers = {"Cache-Control": "no-store"}
    out = None
    try:
        key = hashlib.sha256(f"{p}|{p.stat().st_mtime_ns}|{blur}".encode()).hexdigest()[:24]
        out = cache_dir / f"{key}.jpg"
        return Response(out.read_bytes(), media_type="image/jpeg", headers=headers)
    except OSError:
        pass  # 캐시가 없어도 원본을 읽어 미리보기를 만든다
    try:
        with Image.open(p) as im:
            im = im.convert("RGB")
            im.thumbnail((360, 360))
            if blur:
                im = im.filter(ImageFilter.GaussianBlur(18))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
    except (OSError, SyntaxError, Image.DecompressionBombError):
        log.info("[미리보기] 이미지 읽기 실패 · 묶음 %s · 항목 %s", run_id, entry_id)
        return Response("이미지 미리보기를 열 수 없습니다.", status_code=422,
                        media_type="text/plain", headers=headers)
    data = buf.getvalue()
    if out:
        tmp = out.with_suffix(f".{threading.get_ident()}.tmp")
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, out)  # 원자적 교체 — 쓰는 도중 읽히지 않게
        except OSError:
            log.info("[미리보기] 썸네일 캐시 저장 실패 · 묶음 %s · 항목 %s", run_id, entry_id)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return Response(data, media_type="image/jpeg", headers=headers)


@router.get("/s2/file/{run_id}/{entry_id}")
def s2_file(request: Request, run_id: str, entry_id: str, reveal: int = 0):
    e, p = _entry_file(request, run_id, entry_id)
    if e.get("sensitivity") == "L3" and not reveal:
        raise HTTPException(403, "민감 파일 — 확인 후 보기")
    mt = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    if not (mt.startswith("image/") or mt == "application/pdf"):
        raise HTTPException(415, "브라우저 표시 불가 형식 — 텍스트 미리보기를 사용하세요")
    # inline 표시, 내려받기 강제 아님
    return FileResponse(p, media_type=mt, headers={"Content-Disposition": "inline", "Cache-Control": "no-store",
                                                   "X-Content-Type-Options": "nosniff"})


@router.get("/s2/view/{run_id}/{entry_id}", response_class=HTMLResponse)
def s2_view(request: Request, run_id: str, entry_id: str, reveal: int = 0, from_: str = Query("", alias="from")):
    t = _t(request)
    raw = CatalogStore(t.db.conn).entry(run_id, entry_id)
    section = request.query_params.get("section")
    anchor = section if section in (*ORDER, "shots") else (
        ("shots" if raw.get("screenshot_batch") else raw["verdict"]) if raw else "")
    return_to = request.query_params.get("return_to", "")
    back = "/s2a" if from_ == "s2a" else "/s2/back?" + urlencode(
        {"run": run_id, "to": anchor, **({"return_to": return_to} if return_to else {})})
    reveal_params = {"reveal": "1"}
    if from_ == "s2a":
        reveal_params["from"] = "s2a"
    elif return_to:
        reveal_params["return_to"] = return_to
        reveal_params["section"] = anchor
    if raw and not Path(raw["path"]).exists():  # #7 이미 옮긴 파일: 작업 기록으로 안내
        moved = t.db.conn.execute(
            "SELECT batch_id, dst FROM operations WHERE src=? AND status='DONE' AND (undo_status IS NULL OR undo_status!='UNDONE')"
            " ORDER BY rowid DESC LIMIT 1", (raw["path"],)).fetchone() if _has_ops(t.db.conn) else None
        return templates.TemplateResponse(request, "s2_moved.html", {
            "step": "s2", "e": raw, "batch": moved[0] if moved else None,
            "dst": tilde(Path(moved[1]), t.home) if moved else None, "back": back}, status_code=404)
    e, p = _entry_file(request, run_id, entry_id)
    mt = mimetypes.guess_type(p.name)[0] or ""
    sensitive = e.get("sensitivity") == "L3" and not reveal
    preview = e.get("preview") or []
    if not preview and not sensitive and not (mt.startswith("image/") or mt == "application/pdf"):
        from tongdory2.fsx.extract import extract

        preview = extract(p, e.get("sensitivity", "L1")).preview
    return templates.TemplateResponse(
        request, "s2_view.html",
        {"step": "s2", "e": e, "run_id": run_id, "mt": mt, "sensitive": sensitive, "preview": preview, "reveal": reveal,
         "back": back, "reveal_url": "?" + urlencode(reveal_params)},
    )


def _has_ops(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'").fetchone())


@router.get("/s2/back")
def s2_back(request: Request, to: str = "", run: str = "", return_to: str = ""):
    """마지막으로 본 결과 화면(묶음·탭·쪽)으로 돌아간다. to는 화면 안 위치(#)만.
    #67 파일 상세에서 오면(run) 그 파일이 속한 묶음을 연다 — 다른 탭이 본 묶음으로 가지 않게."""
    t = _t(request)
    url = _review_url(getattr(t, "s2_back", "") or "")
    if run:
        roots, spec = t.db.list_roots(), SpecStore(t.db.conn).current()
        hit = series_run(CatalogStore(t.db.conn), roots, spec, run) if spec else None
        if hit:
            url = _review_url(return_to, notice_of(hit).get("batch_number", 1))
        else:
            url = "/s2?result=1"
            t.flash = "이 파일이 속한 분석 결과는 지금 설정에서 볼 수 없어 최근 결과를 엽니다."
    anchor = to if re.fullmatch(r"[A-Za-z_-]{1,20}", to or "") else ""
    return RedirectResponse(url + (f"#{anchor}" if anchor else ""), status_code=303)


@router.get("/s2/blank")
def s2_blank():
    return Response(status_code=204)
