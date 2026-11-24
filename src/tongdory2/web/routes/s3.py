"""S3 재배치: 계획 미리보기 → 확인 → 실행 → 이력·Undo."""

from __future__ import annotations

from datetime import date
import logging
from collections import Counter, defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from tongdory2.catalog.store import CatalogStore
from tongdory2.fsx.paths import tilde
from tongdory2.fsx.roots import Role, in_git_repo
from tongdory2.handover.checklist import STATUS_LABEL
from tongdory2.handover.final_catalog import batch_folder_results, folder_presence
from tongdory2.plan.executor import batch_ops, complete_review_only, execute, list_batches, load_plan, save_plan, undo
from tongdory2.plan.planner import build_plan, related_saved_versions
from tongdory2.plan.safety import GateContext
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import templates
from tongdory2.web.progress import configured_folders, folder_tree, requires_liner_selection
from tongdory2.web.left_breakdown import for_batch, snapshot
from tongdory2.web.operation_display import operation_display
from tongdory2.web.routes.s4 import collect_final_catalog
from tongdory2.web.selection_form import read_selection
from tongdory2.web.series import decision_fp, done_runs, plan_runs_fresh, series_run
from tongdory2.web.setup_state import ARCHIVE_BUSY, archive_running, current_run, scope_key

router = APIRouter()
log = logging.getLogger(__name__)
ACK_TEXT = {
    "GIT_REPO_DEST": "현재 과제 폴더는 Git 저장소입니다. 옮긴 문서가 커밋될 수 있음을 확인했습니다.",
    "L2_TO_REPO": "계약서·견적서 같은 문서가 저장소 안으로 들어가는 것을 확인했습니다.",
}


def result_tree_rows(ops: list[dict], root: Path | None, spec: dict | None, scope) -> list[dict]:
    """실행 기록에서 실제로 유지 중인 이동·폴더 생성만 세어 결과 폴더 목록을 만든다 (되돌린 항목 제외)."""
    moved: Counter = Counter()
    created = set()
    if root is not None:
        for op in ops:
            if op["status"] != "DONE" or op["undo_status"] == "UNDONE":
                continue
            dst = Path(op["dst"])
            folder = dst.parent
            if op["op"] == "MOVE" and folder.is_relative_to(root):
                moved[folder.relative_to(root).as_posix()] += 1
            elif op["op"] == "MKDIR" and op["created_dir"] and dst.is_relative_to(root):
                created.add(dst.relative_to(root).as_posix())
    rows = folder_tree(configured_folders(spec), moved, proposed=set(), created=created)
    for row in rows:
        row["presence"] = folder_presence(root / row["path"], scope) if root is not None else "unavailable"
    return rows


def _roots(request: Request):
    t = request.app.state.t
    roots = t.db.list_roots()
    cur = next((r for r in roots if r.role == Role.CURRENT and r.confirmed), None)
    store = next((r for r in roots if r.role == Role.STORAGE and r.confirmed), None)
    return t, cur, store


async def _plan_selection(request: Request) -> tuple[dict[str, str], list[str]]:
    return await read_selection(request, "sel", "run_id", optional=("use_saved", "scope"))


@router.post("/s3/plan")
def s3_plan(request: Request, selection: tuple[dict[str, str], list[str]] = Depends(_plan_selection)):
    fields, selected = selection
    run_id = fields["run_id"]
    t = request.app.state.t
    with t.setup_lock:
        _, cur, store = _roots(request)
        roots = t.db.list_roots()
        spec = SpecStore(t.db.conn).current()
        catalog = CatalogStore(t.db.conn)
        run = series_run(catalog, roots, spec, run_id)
        if not (cur and store and run):
            t.flash = "현재 설정으로 분석하고 결과를 다시 확인해 주세요."
            return RedirectResponse("/s2", status_code=303)
        if selected or fields.get("use_saved") != "1":
            # 예전 방식: 이 묶음에서 체크 목록을 보낸 경우
            run_ids = [run_id]
            entries = catalog.entries(run_id)
            marked = set(selected)
        else:
            # 결과 화면은 체크를 서버에 저장한다. 기본은 완료된 묶음 전체를 합쳐서 한 번에 계획한다.
            runs = [run] if fields.get("scope") == "this" else done_runs(catalog, roots, spec)
            run_ids, entries, marked = [], [], set()
            for r in runs:
                rid = r["run_id"]
                rows = catalog.entries(rid)
                chosen = catalog.effective_selection(rid, rows)
                for e in rows:
                    key = f"{rid}:{e['entry_id']}"  # 묶음마다 e0000부터 시작하므로 묶음 id로 구분
                    entries.append(e | {"entry_id": key})
                    if e["entry_id"] in chosen:
                        marked.add(key)
                run_ids.append(rid)
        plan = build_plan(entries, marked, spec["body"]["nodes"], cur.path, store.path, t.home,
                          in_git_repo(cur.path, t.home))
        body = plan.to_dict() | {
            "total": len(entries), "settings_key": scope_key(roots, spec), "runs": run_ids,
            "decision_fp": decision_fp(catalog, run_ids),
            "review_snapshot": snapshot(t.db.conn, entries, marked),
            "unchecked_liner": sum(requires_liner_selection(e) and e["entry_id"] not in marked for e in entries),
            "saved_versions": related_saved_versions(plan, cur, t.scope()) if spec["body"].get("collect_only") else 0,
        }
        pid = save_plan(t.db.conn, run_ids[-1], body)
    return RedirectResponse(f"/s3/plan/{pid}", status_code=303)


@router.get("/s3/plan/{plan_id}", response_class=HTMLResponse)
def s3_plan_view(request: Request, plan_id: str):
    t, cur, store = _roots(request)
    p = load_plan(t.db.conn, plan_id)
    if not p:
        raise HTTPException(404)
    notice, t.flash = t.flash, None
    body = p["body"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for o in body["ops"]:
        if o["op"] == "MKDIR":
            continue
        groups[o["kind"]].append({**o, "src_d": tilde(Path(o["src"]), t.home), "dst_d": tilde(Path(o["dst"]), t.home)})
    by_folder: dict[str, list[dict]] = defaultdict(list)
    for o in groups.get("DEST", []):
        try:
            rel = Path(o["dst"]).parent.relative_to(cur.path).as_posix() if cur else str(Path(o["dst"]).parent)
        except ValueError:
            rel = str(Path(o["dst"]).parent)
        by_folder[rel].append(o)
    spec = SpecStore(t.db.conn).current()
    roots = t.db.list_roots()
    fresh = bool(cur and store and plan_runs_fresh(CatalogStore(t.db.conn), roots, spec, p)
                 and body.get("settings_key") == scope_key(roots, spec))
    total = body.get("total", 0)
    new_dirs = {str(Path(d)) for d in body["mkdirs"]}
    new_rel = {Path(d).relative_to(cur.path).as_posix() for d in new_dirs if cur and Path(d).is_relative_to(cur.path)}
    planned = Counter({p: len(rows) for p, rows in by_folder.items()
                       if cur and not Path(p).is_absolute()})
    n_moves = sum(len(v) for v in groups.values())
    return templates.TemplateResponse(request, "s3_plan.html", {
        "by_folder": dict(sorted(by_folder.items())), "total": total, "n_keep": max(total - n_moves, 0),
        "new_rel": new_rel,
        "tree_rows": folder_tree(configured_folders(spec), planned, proposed=new_rel, created=set()),
        "cur_d": tilde(cur.path, t.home) if cur else "",
        "step": "s3", "plan_id": plan_id, "status": p["status"], "fresh": fresh, "groups": groups, "notice": notice,
        "ready_plan": fresh and p["status"] == "READY",
        "mkdirs": [tilde(Path(d), t.home) for d in body["mkdirs"]], "warnings": body["warnings"],
        "acks": [(a, ACK_TEXT.get(a, a)) for a in body["acks_needed"]], "skipped": body["skipped"],
        "n_moves": n_moves, "extra_ack": _extra_ack(body),
        "unchecked_liner": body.get("unchecked_liner", 0), "saved_versions": body.get("saved_versions", 0),
    })


def _extra_ack(body: dict) -> str:
    """F08: 새 폴더·이름 다듬기가 계획에 있을 때만 받는 추가 확인 문구."""
    new_dirs = bool(body.get("mkdirs"))
    renamed = any("_보관" in (o.get("dst") or "") for o in body.get("ops", []) if o.get("op") == "MOVE")
    if new_dirs and renamed:
        return "새로 만드는 폴더와 다듬은 이름(예: temp → temp_보관)까지 확인했습니다."
    if new_dirs:
        return "새로 만드는 폴더 이름과 위치를 확인했습니다."
    if renamed:
        return "이름 다듬기 규칙(예: temp → temp_보관)을 확인했습니다."
    return ""


@router.post("/s3/execute/{plan_id}")
def s3_execute(request: Request, plan_id: str, acks: list[str] = Form(default=[]), confirm: str = Form(""), allow_empty: str = Form(""),
               extra_confirm: str = Form("")):
    t = request.app.state.t
    with t.setup_lock:
        _, cur, store = _roots(request)
        roots = t.db.list_roots()
        p = load_plan(t.db.conn, plan_id)
        if not p:
            raise HTTPException(404)
        if archive_running(t):
            t.flash = ARCHIVE_BUSY
            return RedirectResponse(f"/s3/plan/{plan_id}", status_code=303)
        spec = SpecStore(t.db.conn).current()
        key = scope_key(roots, spec)
        if not (cur and store and plan_runs_fresh(CatalogStore(t.db.conn), roots, spec, p)
                and p["body"].get("settings_key") == key):
            log.info("[정리실행] 현재 설정과 일치하지 않는 계획 차단")
            t.flash = "계획을 만든 뒤 설정·분석 결과 또는 체크·직접 지정이 바뀌어 실행하지 않았습니다. 검토 화면에서 계획을 다시 만들어 주세요."
            return RedirectResponse("/s2", status_code=303)
        has_moves = any(op["op"] in {"MOVE", "TRASH"} for op in p["body"]["ops"])
        if not has_moves:
            if allow_empty != "1":
                t.flash = "이 계획에는 옮길 파일이 없습니다."
                return RedirectResponse("/s2", status_code=303)
            if confirm != "yes":
                t.flash = "이동 없이 검토를 마치려면 확인에 체크해 주세요."
                return RedirectResponse(f"/s3/plan/{plan_id}", status_code=303)
            try:
                complete_review_only(t.db.conn, plan_id, key)
            except ValueError as exc:
                t.flash = str(exc)
                return RedirectResponse("/s3", status_code=303)
            t.flash = "파일 이동 없이 이번 묶음 검토를 마쳤습니다."
            return RedirectResponse("/", status_code=303)
        if (confirm != "yes" or any(a not in acks for a in p["body"]["acks_needed"])
                or (_extra_ack(p["body"]) and extra_confirm != "yes")):
            t.flash = "옮기기 확인 체크를 모두 마쳐 주세요."
            return RedirectResponse(f"/s3/plan/{plan_id}", status_code=303)
        ctx = GateContext(t.scope(), cur.path, store.path, set(), acks=set(acks), recent_hours=t.settings.recent_hours,
                          settings_key=key)
        try:
            res = execute(t.db.conn, plan_id, ctx)
        except ValueError as exc:
            t.flash = str(exc)
            return RedirectResponse("/s3", status_code=303)
    t.flash = (f"실행 완료: {res.moved}개 이동 · 폴더 {res.created_dirs}개 생성"
               + (f" · 건너뜀 {len(res.skipped)}" if res.skipped else "") + (f" · 차단 {len(res.denied)}" if res.denied else ""))
    return RedirectResponse("/", status_code=303)


@router.get("/s3", response_class=HTMLResponse)
def s3_page(request: Request, batch: str | None = None):
    t, cur, _store = _roots(request)
    notice, t.flash = t.flash, None
    batches = list_batches(t.db.conn)
    selected_batch = next((b for b in batches if b["batch_id"] == batch), None) if batch else (batches[0] if batches else None)
    if batch and not selected_batch:
        notice = (notice + " · " if notice else "") + "요청한 작업 기록을 찾을 수 없습니다. 아래 목록에서 골라 주세요."
    sel = selected_batch["batch_id"] if selected_batch else None
    ops = []
    if sel:
        for o in batch_ops(t.db.conn, sel):
            o["src_d"] = tilde(Path(o["src"]), t.home) if o["src"] else ""
            o["dst_d"] = tilde(Path(o["dst"]), t.home)
            o["display_result"] = operation_display(o)
            ops.append(o)
    spec = SpecStore(t.db.conn).current()
    is_done_view = bool(request.query_params.get("done"))
    can_export_catalog = bool(spec and cur)
    final_catalog = (
        collect_final_catalog(t, sel, strict_batch=True, display_limit=4)
        if (is_done_view and selected_batch and can_export_catalog)
        else None
    )
    default_catalog_stem = (
        f"{cur.path.name}_정리결과_{date.today().strftime('%Y%m%d')}"
        if cur else f"정리결과_{date.today().strftime('%Y%m%d')}"
    )
    scope = t.scope()
    tree_rows = result_tree_rows(ops, cur.path if cur else None, spec, scope)
    result_folders = batch_folder_results(ops, cur.path, scope) if cur and final_catalog else []
    return templates.TemplateResponse(request, "s3.html", {
        "step": "s3", "notice": notice, "batches": batches, "sel": sel, "selected_batch": selected_batch, "ops": ops,
        "tree_rows": tree_rows, "result_folders": result_folders,
        "final_catalog": final_catalog, "can_export_catalog": can_export_catalog,
        "catalog_batch_id": sel or "", "status_labels": STATUS_LABEL,
        "default_catalog_stem": default_catalog_stem,
        "left_breakdown": for_batch(t.db.conn, sel) if selected_batch else None,
    })


@router.post("/s3/undo/{batch_id}")
def s3_undo(request: Request, batch_id: str, confirm: str = Form("")):
    t, _cur, _store = _roots(request)
    if confirm != "yes":
        t.flash = "되돌리기 확인에 체크해 주세요."
        return RedirectResponse(f"/s3?batch={batch_id}", status_code=303)
    try:
        with t.setup_lock:
            if archive_running(t):
                t.flash = ARCHIVE_BUSY
                return RedirectResponse(f"/s3?batch={batch_id}", status_code=303)
            r = undo(t.db.conn, batch_id, t.scope())
    except ValueError as exc:
        t.flash = str(exc)
        return RedirectResponse(f"/s3?batch={batch_id}", status_code=303)
    parts = ([f"{r.restored}개 원위치"] if r.restored else []) + ([f"사본·새 파일 {r.trashed}개 휴지통으로"] if r.trashed else [])
    t.flash = f"되돌리기: {' · '.join(parts) or '변경 없음'} · 빈 폴더 {r.removed_dirs}개 정리" + (
        f" · 멈춤 {len(r.stopped)}개(그사이 바뀐 파일은 건드리지 않음)" if r.stopped else "")
    return RedirectResponse(f"/s3?batch={batch_id}", status_code=303)
