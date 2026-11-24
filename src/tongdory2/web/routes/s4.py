"""인수인계 초안과 지식센터 보관."""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import date
import hashlib
import json
import logging
import re
import threading
from urllib.parse import quote
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from tongdory2.catalog.store import CatalogStore
from tongdory2.fsx.paths import tilde
from tongdory2.fsx.roots import Role
from tongdory2.fsx.scope import Access, Scope
from tongdory2.handover.checklist import STATUS_LABEL, build_checklist
from tongdory2.handover.final_catalog import (
    FinalCatalog,
    build_final_catalog,
    render_final_json,
    render_final_md,
    render_final_txt,
)
from tongdory2.handover.knowledge_center import archive_sensitive, meta_json, plan_copy
from tongdory2.handover.readme import render, summarize, summary_payload
from tongdory2.messages import msg
from tongdory2.plan.executor import batch_ops, copy_batch, list_batches, write_new
from tongdory2.spec.store import SpecStore
from tongdory2.spec.structure_input import sanitize_filename
from tongdory2.web.app import templates
from tongdory2.web.export_io import export_text_or_save
from tongdory2.web.setup_state import approval_token, approved, archive_running, current_run, scope_key

router = APIRouter()
log = logging.getLogger(__name__)
DRAFT_NAME = re.compile(r"README_인수인계(?:_([1-9][0-9]*))?\.md")


def _context(t):
    roots = t.db.list_roots()
    spec = SpecStore(t.db.conn).current()
    cur = next((r for r in roots if r.role == Role.CURRENT and r.confirmed), None)
    store = next((r for r in roots if r.role == Role.STORAGE and r.confirmed), None)
    return (roots, spec, cur, store) if spec and cur and store else None


def _draft_paths(root: Path, scope: Scope) -> list[Path]:
    paths = []
    for p in root.glob("README_인수인계*.md"):
        match = DRAFT_NAME.fullmatch(p.name)
        if match and scope.check(p, Access.READ).allowed and p.is_file():
            paths.append((int(match.group(1) or "0"), p))
    return [p for _, p in sorted(paths, reverse=True)]


def _stamp(t, roots, spec) -> str:
    # 새 묶음, 실행, 되돌리기, 또는 다른 화면의 초안 작성 뒤에는 예전 양식을 다시 제출하지 않는다.
    run = CatalogStore(t.db.conn).latest()
    batches = list_batches(t.db.conn)
    last_batch = batches[0] if batches else None
    last_move = next((b for b in batches if b["run_id"]), None)
    fingerprint = [
        scope_key(roots, spec),
        (run["run_id"], run["status"]) if run else None,
        (last_batch["batch_id"], last_batch["status"]) if last_batch else None,
        (last_move["batch_id"], last_move["status"], last_move["undone_at"]) if last_move else None,
    ]
    return hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()


def _inplace(t, roots, spec) -> tuple[list[dict], dict]:
    catalog = CatalogStore(t.db.conn)
    run = catalog.latest("IN_PLACE")
    if not run or run["status"] != "DONE" or run["spec_version"] != spec["version"]:
        return [], {}
    scope = Scope(roots, t.home, t.app_data)
    entries = [e for e in catalog.entries(run["run_id"])
               if (check := scope.check(e["path"], Access.READ)).allowed and check.root.role == Role.IN_PLACE]
    return entries, json.loads(run["notice"] or "{}") if entries else {}


def collect_final_catalog(
    t,
    batch_id: str | None = None,
    *,
    strict_batch: bool = False,
    display_limit: int | None = None,
) -> FinalCatalog | None:
    roots = t.db.list_roots()
    spec = SpecStore(t.db.conn).current()
    cur = next((r for r in roots if r.role == Role.CURRENT and r.confirmed), None)
    if not (spec and cur):
        return None
    scope = Scope(roots, t.home, t.app_data)
    catalog = CatalogStore(t.db.conn)
    ip, _ = _inplace(t, roots, spec)
    batches = list_batches(t.db.conn)
    selected = None
    if batch_id:
        selected = next((b for b in batches if b["batch_id"] == batch_id), None)
    if selected is None and not strict_batch and batches:
        selected = next((b for b in batches if not b["summary"].get("label")), batches[0])
    ops = batch_ops(t.db.conn, selected["batch_id"]) if selected else []
    from tongdory2.web.left_breakdown import for_batch
    breakdown = for_batch(t.db.conn, selected["batch_id"]) if selected else None
    hold_count = 0
    left_count = 0
    latest_run = catalog.latest()
    if current_run(latest_run, roots, spec):
        entries = catalog.entries(latest_run["run_id"])
        hold_count = sum(1 for e in entries if e.get("verdict") == "HOLD" and e.get("sensitivity") != "L3")
        if selected and selected.get("status") == "UNDONE":
            active_moved = 0
        elif ops:
            active_moved = sum(
                1 for op in ops if op.get("op") == "MOVE" and op.get("status") == "DONE" and op.get("undo_status") != "UNDONE"
            )
        else:
            active_moved = int((selected or {}).get("summary", {}).get("moved") or 0)
        left_count = max(len(entries) - active_moved, 0)
    if breakdown:
        left_count = breakdown["left"]
    return build_final_catalog(
        spec,
        cur.path,
        t.home,
        scope,
        inplace_entries=ip,
        sensitive_paths=catalog.sensitive_paths(),
        selected_batch=selected,
        ops=ops,
        hold_count=hold_count,
        left_count=left_count,
        display_limit=display_limit,
    )


def _render_catalog_by_fmt(cat: FinalCatalog, fmt: str) -> str:
    if fmt == "json":
        return render_final_json(cat)
    if fmt == "txt":
        return render_final_txt(cat)
    return render_final_md(cat)


@router.get("/s4", response_class=HTMLResponse)
def s4_page(request: Request, archive: bool = False, preview_liner: bool = False):
    t = request.app.state.t
    ctx = _context(t)
    if not ctx:
        t.flash = "저장 위치와 폴더 목록을 먼저 확정해 주세요."
        return RedirectResponse("/", status_code=303)
    roots, spec, cur, store = ctx
    job = getattr(t, "handover_job", {})
    if job.get("key") != scope_key(roots, spec):
        job = {}
    if job.get("running"):
        return templates.TemplateResponse(request, "s4_progress.html", {"job": job.copy()})
    if job.get("filename") and not job.get("shown"):  # 작성이 끝나면 결과(초안 보기)를 한 번 보여 준다
        job["shown"] = True
        return RedirectResponse(f"/s4/draft/{quote(job['filename'])}", status_code=303)
    notice, t.flash = t.flash, None
    scan_scope = Scope(roots, t.home, t.app_data)
    ip, ip_sum = _inplace(t, roots, spec)
    rows = build_checklist(spec, cur.path, ip, scan_scope)
    kp = (plan_copy(cur.path, store.path, ip, spec["version"], scope=scan_scope,
                    sensitive_paths=CatalogStore(t.db.conn).sensitive_paths()) if archive else None)
    req = [r for r in rows if r.required]
    drafts = _draft_paths(cur.path, scan_scope)
    stamp = _stamp(t, roots, spec)
    payload = None
    if preview_liner and t.settings.liner_api_key:
        payload = summary_payload(cur.path, rows, ip, scan_scope, CatalogStore(t.db.conn).sensitive_paths())
    kc_batches = [b for b in list_batches(t.db.conn) if b["summary"].get("label") == "지식센터 보관"][:10]
    default_catalog_stem = f"{cur.path.name}_정리결과_{date.today().strftime('%Y%m%d')}"
    return templates.TemplateResponse(request, "s4.html", {
        "step": "s4", "notice": notice, "rows": rows, "labels": STATUS_LABEL,
        "cur": tilde(cur.path, t.home), "drafts": [p.name for p in drafts],
        "req_total": len(req), "req_ok": sum(1 for r in req if r.status == "PRESENT"),
        "has_key": bool(t.settings.liner_api_key), "draft_stamp": stamp, "job": job,
        "kc_job": t.kc_job if archive_running(t) else None,
        "payload": payload, "approval": approval_token(t, "handover", stamp, payload) if payload else "",
        "ip_sum": ip_sum, "kp": kp, "kc_dest": tilde(Path(kp.dest_root), t.home) if kp else None,
        "kc_batches": kc_batches, "final_catalog": None, "can_export_catalog": True,
        "catalog_batch_id": "", "default_catalog_stem": default_catalog_stem,
    })


@router.get("/s4/catalog/summary")
def s4_catalog_summary(request: Request, batch: str | None = None):
    cat = collect_final_catalog(request.app.state.t, batch, strict_batch=bool(batch), display_limit=4)
    if not cat:
        return JSONResponse({"error": msg("catalog.err.need_setup"), "code": "catalog.err.need_setup"}, status_code=422)
    parts = [
        f"폴더 {cat.total_folders}개",
        f"파일 {cat.total_files}개",
        f"필수 산출물 확인 {cat.required_ok}/{cat.required_total}개",
    ]
    if cat.excluded_sensitive:
        parts.append(f"민감 파일 {cat.excluded_sensitive}개 제외")
    return JSONResponse({
        "summary_text": " · ".join(parts),
        "is_undone": cat.is_undone,
        "is_partial_undo": cat.is_partial_undo,
        "undone_count": cat.undone_count,
        "truncated_folders": cat.truncated_folders,
    })


@router.get("/s4/catalog/preview")
def s4_catalog_preview(request: Request, fmt: str = "md", batch: str | None = None):
    fmt_clean = fmt.strip().lower()
    if fmt_clean not in ("md", "json", "txt"):
        return PlainTextResponse(msg("catalog.err.unsupported_preview_fmt"), status_code=422)
    cat = collect_final_catalog(request.app.state.t, batch, strict_batch=bool(batch))
    if not cat:
        return PlainTextResponse(msg("catalog.err.need_setup"), status_code=422)
    return PlainTextResponse(
        _render_catalog_by_fmt(cat, fmt_clean),
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/s4/catalog/export")
def s4_catalog_export(
    request: Request,
    fmt: str = Form("md"),
    filename: str = Form(""),
    save_dir: str = Form(""),
    batch: str = Form(""),
):
    t = request.app.state.t
    fmt_clean = fmt.strip().lower()
    if fmt_clean not in ("md", "json", "txt"):
        return JSONResponse({"error": msg("catalog.err.unsupported_fmt"), "code": "catalog.err.unsupported_fmt"}, status_code=422)
    batch_clean = batch.strip() or None
    cat = collect_final_catalog(t, batch_clean, strict_batch=bool(batch_clean))
    if not cat:
        return JSONResponse({"error": msg("catalog.err.need_setup"), "code": "catalog.err.need_setup"}, status_code=422)
    try:
        content = _render_catalog_by_fmt(cat, fmt_clean)
        default_stem = f"{cat.project}_정리결과_{date.today().strftime('%Y%m%d')}"
        clean_name = sanitize_filename(filename, fmt_clean, default_stem)
        return export_text_or_save(content, clean_name, fmt_clean, save_dir, t)
    except (ValueError, OSError) as error:
        log.info("[최종목록표] 저장 실패 (%s)", type(error).__name__)
        return JSONResponse({"error": str(error)}, status_code=422)


@router.post("/s4/readme")
def s4_readme(request: Request, stamp: str = Form(...), use_liner: bool = Form(False), approval: str = Form("")):
    t = request.app.state.t
    with t.setup_lock:
        ctx = _context(t)
        if not ctx:
            return RedirectResponse("/", status_code=303)
        roots, spec, cur, _ = ctx
        if getattr(t, "handover_job", {}).get("running"):
            return RedirectResponse("/s4", status_code=303)
        if stamp != _stamp(t, roots, spec):
            t.flash = "인수인계 정보가 갱신되었습니다. 현재 내용과 작성 결과를 확인해 주세요."
            return RedirectResponse("/s4", status_code=303)
        if use_liner and not t.settings.liner_api_key:
            t.flash = "AI 요약을 쓰려면 Liner API 키를 설정해 주세요."
            return RedirectResponse("/s4", status_code=303)
        if use_liner and not approval:
            t.flash = "Liner로 다듬기 전에 보낼 내용을 확인해 주세요. 아래 [Liner에 보내고 초안 작성]을 누르면 진행합니다."
            return RedirectResponse("/s4?preview_liner=1#liner-preview", status_code=303)
        job = {"running": True, "key": scope_key(roots, spec), "stage": "저장 폴더 확인 중"}
        t.handover_job = job

    def worker():
        try:
            scope = Scope(roots, t.home, t.app_data)
            ip, ip_sum = _inplace(t, roots, spec)
            rows = build_checklist(spec, cur.path, ip, scope)
            catalog = CatalogStore(t.db.conn)
            run = catalog.latest()
            analysis = json.loads(run["notice"] or "{}") if current_run(run, roots, spec) else None
            holds = [e for e in catalog.entries(run["run_id"])
                     if e["verdict"] == "HOLD" and e.get("sensitivity") != "L3"] if analysis else []
            summary = None
            if use_liner:
                payload = summary_payload(cur.path, rows, ip, scope, catalog.sensitive_paths())
                with t.setup_lock:
                    current = _context(t)
                    if (not current or stamp != _stamp(t, current[0], current[1]) or not payload
                            or not approved(t, approval, "handover", stamp, payload)):
                        job["error"] = "전송할 내용이 바뀌었거나 승인되지 않았습니다. 현재 목록을 미리 확인해 주세요."
                        log.info("[인수인계] 전송 미리보기와 일치하지 않아 Liner 요청 차단")
                        return
                job["stage"] = "Liner 요약 만들기"
                from tongdory2.liner.client import LinerClient

                async def go():
                    async with LinerClient(t.settings.liner_api_key, t.settings.liner_base_url,
                                           conn=t.db.conn, run_id="handover") as client:
                        return await asyncio.wait_for(summarize(client, payload), timeout=30)

                try:
                    summary = asyncio.run(go())
                except Exception as error:
                    job["note"] = "AI 요약을 가져오지 못해 로컬 요약으로 작성했습니다."
                    log.warning("[인수인계] Liner 요약 실패 (%s)", type(error).__name__)

            job["stage"] = "마크다운 저장 중"
            content = render(cur.path.name, rows, summary, holds, ip_sum, None, analysis=analysis)
            with t.setup_lock:
                current = _context(t)
                if not current or stamp != _stamp(t, current[0], current[1]):
                    job["error"] = "작성 중 설정이나 실행 결과가 바뀌었습니다. 현재 상태로 다시 작성해 주세요."
                    return
                bid, path = write_new(t.db.conn, cur.path / "README_인수인계.md", content,
                                      t.scope(), t.home, "인수인계 초안")
            job.update(batch_id=bid, filename=path.name)
            log.info("[인수인계] 초안 작성 완료 (%d개 산출물)", len(rows))
        except Exception:
            job["error"] = "초안을 작성하지 못했습니다. 저장 폴더의 쓰기 권한을 확인해 주세요."
            log.exception("[인수인계] 초안 작성 실패")
        finally:
            job["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return RedirectResponse("/s4", status_code=303)


@router.get("/s4/draft/{name}")
def s4_draft(request: Request, name: str, download: bool = False):
    t = request.app.state.t
    ctx = _context(t)
    if not ctx or not DRAFT_NAME.fullmatch(name):
        raise HTTPException(404)
    path = ctx[2].path / name
    if not t.scope().check(path, Access.READ).allowed or not path.is_file():
        raise HTTPException(404)
    if download:
        return FileResponse(path, filename=name, media_type="text/plain",
                            content_disposition_type="attachment",
                            headers={"X-Content-Type-Options": "nosniff"})
    content = path.read_text(encoding="utf-8", errors="replace")
    return templates.TemplateResponse(
        request, "s4_draft.html", {"filename": name, "content": content},
        headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/s4/kc")
def s4_kc(request: Request):
    t = request.app.state.t
    with t.setup_lock:
        if archive_running(t):
            return RedirectResponse("/s4?archive=1#kc", status_code=303)
        ctx = _context(t)
        if not ctx:
            return RedirectResponse("/", status_code=303)
        roots, spec, cur, store = ctx
        ip, _ = _inplace(t, roots, spec)
        scope = Scope(roots, t.home, t.app_data)
        sensitive_paths = CatalogStore(t.db.conn).sensitive_paths()
        kp = plan_copy(cur.path, store.path, ip, spec["version"], scope=scope, sensitive_paths=sensitive_paths)
        recovered = 0

        def archive_metadata(copied_paths: set[str]) -> dict[str, str]:
            nonlocal recovered
            recovered = len(kp.recovered_destinations & copied_paths)
            return {str(Path(kp.dest_root) / "kc_meta.json"): meta_json(kp, copied_paths)}

        job = {"running": True, "done": 0, "total": len(kp.pairs)}
        t.kc_job = job

    def worker() -> None:  # 파일이 많으면 오래 걸린다 — 뒤에서 복사하고 화면은 진행 상황을 보여 준다
        import sqlite3

        try:
            with closing(sqlite3.connect(str(t.settings.db_path), timeout=10)) as conn:
                _, st = copy_batch(conn, kp.pairs, kp.mkdirs, scope, "지식센터 보관", archive_metadata,
                                   source_allowed=lambda path, head: not archive_sensitive(path, sensitive_paths, head),
                                   progress=lambda done, total: job.update(done=done, total=total))
            skipped = f" · 복사 직전 확인에서 {st['skipped']}개 건너뜀" if st["skipped"] else ""
            metadata = " · 보관 정보 저장 실패" if st.get("metadata_failed") else ""
            undo_hint = " 보관 기록에서 되돌릴 수 있습니다." if st["copied"] or st["created_dirs"] else ""
            t.flash = (f"지식센터에 {st['copied']}개 복사 · Git에 없던 AI 협업 문서 {recovered}개 회수{skipped}{metadata} · "
                       f"{tilde(Path(kp.dest_root), t.home)}.{undo_hint}")
        except OSError as exc:
            t.flash = f"보관할 수 없습니다: {exc}"
        except Exception:
            log.exception("[지식센터] 복사 실패")
            t.flash = "지식센터 복사 중 오류가 발생했습니다. 작업 기록을 확인해 주세요."
        finally:
            job.update(running=False, done=job["total"])

    import contextvars

    # 안내 메시지(flash)는 브라우저별로 저장된다 — 요청의 문맥을 그대로 넘겨 결과 안내가 이 브라우저에 뜨게 한다
    try:
        threading.Thread(target=contextvars.copy_context().run, args=(worker,), daemon=True).start()
    except RuntimeError:
        log.exception("[지식센터] 복사 작업 시작 실패")
        t.flash = "지식센터 복사를 시작하지 못했습니다. 다시 시도해 주세요."
        job["running"] = False
    return RedirectResponse("/s4?archive=1#kc", status_code=303)
