"""문제점 이력 #51·#52 및 #76~#88의 재현 경로."""

import html
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from tests.test_grouping import _one_file_app, _plan
from tests.test_survey_filter import _raw_get, _setup, _wait
from tongdory2.catalog.survey import FilterStore
from tongdory2.config import Settings
from tongdory2.fsx.roots import Role
from tongdory2.handover.docs_index import render
from tongdory2.plan.executor import list_batches
from tongdory2.spec.store import SpecStore
from tongdory2.web import setup_state
from tongdory2.web.app import create_app
from tongdory2.web.routes import home, s2, s4
from tongdory2.web.series import notice_of
from tongdory2.web.setup_state import ARCHIVE_BUSY


@pytest.mark.one_batch
def test_new_run_reconfirms_all_three_steps_and_invalidates_old_plan(tmp_path):
    app, client, source, _, store = _one_file_app(tmp_path)
    run_id = store.latest()["run_id"]
    old_plan = _plan(client, run_id)
    get_storage = lambda: next(r for r in app.state.t.db.list_roots() if r.role == Role.STORAGE)
    storage_id = get_storage().root_id
    client.post("/new-run", data={"confirm": "yes"}, follow_redirects=False)
    assert "설정 0/3" in client.get("/").text
    assert "보고/회의록" not in client.get("/setup/structure").text
    assert not any(r.role in (Role.COLLECT, Role.CURRENT) for r in app.state.t.db.list_roots())
    assert client.get("/s2", follow_redirects=False).headers["location"] == "/setup/structure"
    assert client.get("/setup/destination", follow_redirects=False).headers["location"] == "/setup/structure"
    client.post("/setup/structure", data={"paths": "보고/회의록\n설계"})
    assert "설정 1/3" in client.get("/").text
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    assert "설정 2/3" in client.get("/").text
    client.post("/setup/destination", data={"path": "~/Documents/정리"})
    assert "설정 3/3" in client.get("/").text
    assert get_storage().confirmed and get_storage().root_id == storage_id
    assert client.post(f"/s3/execute/{old_plan}", data={"confirm": "yes"}, follow_redirects=False).headers["location"] == "/s2"
    assert store.get(run_id) and source.exists()


@pytest.mark.one_batch
def test_windows_upgrade_requires_reanalysis_before_using_an_old_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_state, "_WINDOWS", False)
    app, client, source, _, store = _one_file_app(tmp_path)
    conn = app.state.t.db.conn
    run = store.latest()
    plan_id = _plan(client, run["run_id"])
    roots = app.state.t.db.list_roots()
    spec = SpecStore(conn).current()
    assert setup_state.current_run(run, roots, spec)

    monkeypatch.setattr(setup_state, "_WINDOWS", True)
    assert not setup_state.current_run(run, roots, spec)
    response = client.post(f"/s3/execute/{plan_id}", data={"confirm": "yes"}, follow_redirects=False)
    assert response.headers["location"] == "/s2"
    assert source.exists() and store.get(run["run_id"]) and not list_batches(conn)


@pytest.mark.one_batch
def test_two_resets_in_the_same_second_keep_both_backups(tmp_path, monkeypatch):
    app, client, source, _, _ = _one_file_app(tmp_path)

    class FixedDateTime:
        @staticmethod
        def now():
            return datetime(2026, 11, 20, 11, 0)

    monkeypatch.setattr(home, "datetime", FixedDateTime)
    client.post("/demo-reset", data={"confirm": "yes"})
    client.post("/demo-reset", data={"confirm": "yes"})
    root_counts = []
    for backup in (app.state.t.db.path.parent / "backups").glob("tongdory2-20261120-110000-*.db"):
        with sqlite3.connect(backup) as conn:
            root_counts.append(conn.execute("SELECT COUNT(*) FROM roots").fetchone()[0])
    assert sorted(root_counts) == [0, 3]
    assert source.exists()


def test_empty_filter_keeps_prompt_and_last_saved_filter(tmp_path):
    app, client = _setup(tmp_path)
    _raw_get(client, "/s2?new=1")
    page = _wait(client, 'action="/s2/filter"')
    token = re.search(r'name="token" value="([^"]+)"', page)[1]
    filters = FilterStore(app.state.t.db.conn)
    assert filters.load() is None
    resp = client.post("/s2/filter", data={"token": token})
    assert "분류할 파일이 0개입니다" in resp.text and 'name="prep_token"' not in resp.text
    assert filters.load() is None and app.state.t.prep_job["awaiting_filter"]
    client.post("/s2/filter", data={"token": token, "keep_ext": "txt"})
    _wait(client, 'name="prep_token"')


def _batch(store, base, number, rows):
    run_id = store.start(base["spec_version"], notice_of(base) | {"batch_number": number})
    store.finish(run_id, rows, 0.0)
    return run_id


@pytest.mark.one_batch
def test_bulk_selection_matches_missing_automatic_destinations_only(tmp_path):
    app, client, source, destination, store = _one_file_app(tmp_path)
    base = store.latest()
    entry = store.entries(base["run_id"])[0]
    spec = SpecStore(app.state.t.db.conn).current()
    node_id = next(n["node_id"] for n in spec["body"]["nodes"] if n["display_path"] == "설계")
    rows = []
    for eid, verdict, folder in (
        ("group", "GROUP", "새 묶음"), ("spec", "SPEC_NODE", "누락된 기준"),
        ("proposal", "NEW_NODE", "새 제안"), ("existing", "GROUP", "설계"),
        ("manual", "GROUP", "새 묶음"), ("misc", "ETC", "기타"), ("dup", "DUP", None),
    ):
        path = source.with_name(f"{eid}.txt")
        path.write_text("가상 자료", encoding="utf-8")
        rows.append(entry | {"entry_id": eid, "path": str(path), "name": path.name, "verdict": verdict,
                             "node_path": folder, "default_checked": True})
    run_id = _batch(store, base, 1, rows)
    other = _batch(store, base, 2, [rows[0]])
    store.set_override(run_id, ["manual"], node_id)
    url = "/s2?result=1&batch=1&tab=check"
    assert "이번 분류 묶음 · 새로 만들 폴더 3곳 · 선택 파일 3개" in client.get(url).text
    old_plan = _plan(client, run_id)
    plan = client.get(f"/s3/plan/{old_plan}").text
    assert re.search(r'aria-current="step"[^>]*>\s*<span.*?정리 실행', plan, re.S)
    changed = client.post("/s2/check", data={"run_id": run_id, "entry_id": "proposal", "checked": "0", "preview": "1"}).json()
    assert changed["new_files"] == 2 and changed["tree_counts"]["새 제안"] == 0
    current_plan = _plan(client, run_id)
    resp = client.post("/s2/check", data={"run_id": run_id, "action": "leave_check", "return_to": url})
    assert "2개 파일의 이동 선택을 해제했습니다" in resp.text
    assert store.effective_selection(run_id) == {"existing", "manual", "misc", "dup"}
    assert store.effective_selection(other) == {"group"}
    assert (destination / "설계").is_dir() and not (destination / "새 묶음").exists()
    assert "이 계획은 실행할 수 없습니다" in client.get(f"/s3/plan/{current_plan}").text
    client.post("/s2/assign", data={"run_id": run_id, "entry_ids": "manual", "target": "CLEAR"})
    assert "manual" not in store.effective_selection(run_id)
    assert not next(e for e in store.entries(run_id) if e["entry_id"] == "manual")["override"]


@pytest.mark.one_batch
def test_result_and_file_back_keep_tab_and_page_during_next_inspection(tmp_path, monkeypatch):
    app, client, _, _, store = _one_file_app(tmp_path)
    t = app.state.t
    base = store.latest()
    entry = store.entries(base["run_id"])[0]
    run_id = _batch(store, base, 1, [entry | {"entry_id": f"file{i}"} for i in range(3)])
    monkeypatch.setattr(s2, "PAGE_SIZE", 1)
    origin = "/s2?result=1&batch=1&tab=plan&page=3"
    t.prep_job = {"running": True, "stage": "로컬 검사", "done": 1, "total": 5}
    assert "파일 분류 중" in client.get("/s2").text
    page = client.get(origin).text
    assert "3 / 3쪽" in page
    link = html.unescape(re.search(r'href="(/s2/view/[^"]+)"', page)[1])
    detail = client.get(link).text
    back = html.unescape(re.search(r'href="(/s2/back[^"]+)"', detail)[1])
    client.get("/s2?result=1&batch=1&tab=check")
    assert client.get(back, follow_redirects=False).headers["location"].split("#")[0] == origin
    t.job = {"running": False, "ticket": "test-complete", "suggested": True, "return_to": origin, "batch_number": 1}
    result = client.get(origin + "&document_job=test-complete", follow_redirects=False)
    assert result.headers["location"] == origin + "&liner_result=test-complete"
    assert "Liner 제안 보기" in client.get(result.headers["location"]).text
    assert run_id in link


@pytest.mark.one_batch
def test_corrupt_thumbnail_returns_feedback_and_allows_retry(tmp_path):
    from PIL import Image

    _, client, source, _, store = _one_file_app(tmp_path)
    run_id = store.latest()["run_id"]
    entry = store.entries(run_id)[0]
    url = f"/s2/thumb/{run_id}/{entry['entry_id']}"
    assert client.get(url).status_code == 422
    Image.new("RGB", (20, 20)).save(source, "PNG")
    assert client.get(url).headers["content-type"] == "image/jpeg"


def test_index_ai_config_only_relative_path_and_sensitive_only_count(tmp_path):
    repo = tmp_path / "협업"
    content = render(
        [
            {"kind": "text", "reason_code": "AI_CONTEXT", "path": str(repo / "AGENTS.md"), "title": "도구 설정 제목"},
            {"kind": "text", "reason_code": "SENSITIVE", "path": str(repo / "보호 문서.md"), "title": "보호 문서 제목"},
            {"kind": "text", "path": str(repo / "회의록.md"), "title": "회의록", "doc_type": "OTHER"},
        ],
        dict(md_total=3, not_in_git=0, ignored=0, untracked=0, this_month=0),
        repo, tmp_path, "가상 과제",
    )
    assert "AI 도구 설정 파일 1개" in content and "    협업/AGENTS.md" in content
    assert "[회의록](협업/회의록.md)" in content and "민감 문서 1개" in content
    assert all(s not in content for s in ("도구 설정 제목", "](협업/AGENTS.md)", "보호 문서"))


@pytest.mark.one_batch
def test_knowledge_archive_checks_current_content_and_known_classification(tmp_path):
    app, client, _, destination, store = _one_file_app(tmp_path)
    folder = destination / "설계"
    sensitive = b"-----BEGIN PRIVATE KEY-----\nDEMO-ONLY-NOT-A-KEY\n-----END PRIVATE KEY-----"
    (folder / "일반.txt").write_text("가상 일반 문서", encoding="utf-8")
    (folder / "본문보호.txt").write_bytes(sensitive)
    (folder / "판정보호.txt").write_text("집계의 이전 민감 판정을 검증하는 가상 문서", encoding="utf-8")
    (folder / "변경문서.txt").write_text("미리보기 시점에는 일반 문서", encoding="utf-8")
    (folder / "AGENTS.md").write_text("가상 AI 설정", encoding="utf-8")
    run_id = store.latest()["run_id"]
    store.finish(run_id, [{
        "entry_id": "protected", "path": str(folder / "판정보호.txt"),
        "verdict": "EXCLUDE", "sensitivity": "L3",
    }], 0)
    preview = client.get("/s4?archive=1")
    assert "2개 (과제 문서 2" in preview.text
    assert {entry["reason"] for entry in preview.context["kp"].excluded} == {"민감", "AI 설정"}
    (folder / "변경문서.txt").write_bytes(sensitive)
    client.post("/s4/kc", follow_redirects=False)
    import time as _t
    for _ in range(200):  # 복사는 뒤에서 돈다 — 끝나면 결과 안내
        if not (getattr(app.state.t, "kc_job", None) or {}).get("running"):
            break
        _t.sleep(0.02)
    done = client.get("/s4?archive=1")
    assert "지식센터에 1개 복사" in done.text
    storage = next(root for root in app.state.t.db.list_roots() if root.role == Role.STORAGE)
    copied = {path.name for path in storage.path.rglob("*") if path.is_file()}
    assert copied == {"일반.txt", "kc_meta.json"}
    assert (folder / "변경문서.txt").read_bytes() == (folder / "본문보호.txt").read_bytes() == sensitive


@pytest.mark.one_batch
def test_knowledge_archive_prevents_conflicting_requests_until_copy_finishes(tmp_path, monkeypatch):
    app, client, source, destination, store = _one_file_app(tmp_path)
    t = app.state.t
    plan_id = _plan(client, store.latest()["run_id"])
    roots = t.db.list_roots()
    storage = next(root.path for root in roots if root.role == Role.STORAGE)
    for name in ("일반1.txt", "일반2.txt"):
        (destination / "설계" / name).write_text("가상 일반 문서", encoding="utf-8")
    paused, release = Event(), Event()
    copy_batch = s4.copy_batch

    def pause_after_first_copy(*args, progress, **kwargs):
        def on_progress(done, total):
            progress(done, total)
            if done == 1:
                paused.set()
                assert release.wait(timeout=10)

        return copy_batch(*args, progress=on_progress, **kwargs)

    monkeypatch.setattr(s4, "copy_batch", pause_after_first_copy)
    client.post("/s4/kc", follow_redirects=False)
    try:
        assert paused.wait(timeout=5)
        batch, = list_batches(t.db.conn)
        batch_id = batch["batch_id"]
        assert batch["status"] == "RUNNING" and len(list(storage.rglob("*.txt"))) == 1
        for route in ("/demo-reset", "/new-run"):
            assert ARCHIVE_BUSY in client.post(route, data={"confirm": "yes"}).text
        blocked = client.post("/setup/structure", data={"paths": "추가폴더"})
        assert blocked.status_code == 422 and ARCHIVE_BUSY in blocked.text
        for route in (f"/s3/undo/{batch_id}", f"/s3/execute/{plan_id}"):
            assert ARCHIVE_BUSY in client.post(route, data={"confirm": "yes"}).text
        client.post("/s4/kc", follow_redirects=False)
        assert t.db.list_roots() == roots and list_batches(t.db.conn) == [batch]
        assert source.exists() and len(list(storage.rglob("*.txt"))) == 1
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while t.kc_job["running"] and time.monotonic() < deadline:
            time.sleep(0.02)

    assert t.kc_job["running"] is False
    done, = list_batches(t.db.conn)
    assert done["batch_id"] == batch_id and done["status"] == "DONE"
    assert {p.name for p in storage.rglob("*.txt")} == {"일반1.txt", "일반2.txt"}
    client.post(f"/s3/undo/{batch_id}", data={"confirm": "yes"})
    assert list_batches(t.db.conn)[0]["status"] == "UNDONE"
    assert not list(storage.rglob("*.txt")) and len(list((destination / "설계").glob("*.txt"))) == 2


@pytest.mark.one_batch
def test_standard_search_remembers_home_or_exact_review_origin(tmp_path):
    _, client, _, _, _ = _one_file_app(tmp_path)
    home = client.get("/s2/standards", params={"identifier": "RFC 8446", "return_to": "/"}).text
    assert 'href="/">상황판으로 돌아가기' in home
    review = "/s2?result=1&batch=1&tab=plan&page=3"
    query = {"identifier": "RFC 8446", "return_to": review}
    for page in (
        client.get("/s2/standards", params=query),
        client.post("/s2/standards", data=query | {"approval": "outdated"}),
    ):
        assert f'href="{review}">검토 화면으로 돌아가기' in html.unescape(page.text)
    external = client.get("/s2/standards", params={"return_to": "//example.invalid/redirect"})
    assert 'href="/s2?result=1">검토 화면으로 돌아가기' in external.text


@pytest.mark.one_batch
def test_group_card_selection_update_contains_only_affected_counts(tmp_path):
    _, client, _, _, store = _one_file_app(tmp_path)
    base = store.latest()
    entry = store.entries(base["run_id"])[0]
    entries = [
        entry | {"entry_id": key, "verdict": "GROUP", "node_path": key,
                 "group_key": key, "group_kind": "folder", "default_checked": True}
        for key in ("선택 변경", "기존 묶음")
    ]
    run_id = _batch(store, base, 1, entries)
    page = client.get("/s2?result=1&tab=plan").text
    assert 'data-group-key="선택 변경"' in page and "1/1 선택" in page
    fields = {"run_id": run_id, "entry_id": "선택 변경", "preview": "1"}
    for checked in ("0", "1"):
        res = client.post("/s2/check", data=fields | {"checked": checked}).json()
        assert res["group_counts"] == {"선택 변경": int(checked)}
        assert res["checked"] == 1 + int(checked)


def test_liner_probe_reports_missing_key_before_requests(tmp_path):
    probe = tmp_path / "project" / "scripts" / "probe_liner.py"
    probe.parent.mkdir(parents=True)
    probe.write_bytes(Path("scripts/probe_liner.py").read_bytes())
    result = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                            env=os.environ | {"LINER_API_KEY": ""}, timeout=10, check=False)
    assert result.returncode == 1 and "[Liner 진단]" in result.stderr
    assert "LINER_API_KEY" in result.stderr and not result.stdout


@pytest.mark.one_batch
def test_new_run_clears_home_setup_state_and_preserves_history_for_undo(tmp_path):
    app, client, source, dest, store = _one_file_app(tmp_path)
    run_id = store.latest()["run_id"]
    plan_id = _plan(client, run_id)
    client.post(f"/s3/execute/{plan_id}", data={"confirm": "yes", "extra_confirm": "yes"})
    assert not source.exists() and (dest / "보고" / "회의록" / source.name).exists()
    batches_before = list_batches(app.state.t.db.conn)
    assert len(batches_before) == 1
    batch_id = batches_before[0]["batch_id"]

    res = client.post("/new-run", data={"confirm": "yes"}, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/"
    home_html = client.get("/").text
    assert "설정 0/3" in home_html
    assert not re.findall(r'href="/setup/[^"]+">수정</a>', home_html)
    assert "다시 확정 필요" not in home_html
    assert 'id="new-run"' in home_html
    assert store.get(run_id) is not None
    assert [b["batch_id"] for b in list_batches(app.state.t.db.conn)] == [batch_id]

    # 새 정리에서 같은 폴더를 다시 확정한 뒤에도 이전 배치를 undo()로 정상 복원할 수 있다
    client.post("/setup/structure", data={"paths": "보고/회의록\n설계"})
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    client.post("/setup/destination", data={"path": "~/Documents/정리"})
    client.post(f"/s3/undo/{batch_id}", data={"confirm": "yes"}, follow_redirects=False)
    assert source.exists() and not (dest / "보고" / "회의록" / source.name).exists()


def test_home_edit_links_appear_only_for_confirmed_steps(tmp_path):
    home_dir = tmp_path.resolve() / "home"
    (home_dir / "Desktop").mkdir(parents=True)
    (home_dir / "Documents" / "정리").mkdir(parents=True)
    app = create_app(Settings(TONGDORY_HOME=str(home_dir), TONGDORY_DATA_DIR=str(tmp_path / "app"), LINER_API_KEY=""))
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    edit_links = lambda body: re.findall(r'href="(/setup/[^"]+)">수정</a>', body)

    step0 = client.get("/").text
    assert "설정 0/3" in step0 and edit_links(step0) == [] and 'id="new-run"' in step0

    client.post("/setup/structure", data={"paths": "보고/회의록\n설계"})
    step1 = client.get("/").text
    assert "설정 1/3" in step1 and edit_links(step1) == ["/setup/structure"]

    client.post("/setup/sources", data={"paths": "~/Desktop"})
    step2 = client.get("/").text
    assert "설정 2/3" in step2 and edit_links(step2) == ["/setup/structure", "/setup/sources"]

    client.post("/setup/destination", data={"path": "~/Documents/정리"})
    step3 = client.get("/").text
    assert "설정 3/3" in step3 and edit_links(step3) == [
        "/setup/structure",
        "/setup/sources",
        "/setup/destination",
    ]


@pytest.mark.one_batch
def test_home_edit_query_skips_readonly_and_past_lock(tmp_path):
    """분석 시작 전에는 /setup/*가 잠금 없이 수정 가능하고, 분석 시작·완료 후에는 보기/읽기 전용으로 잠긴다."""
    import time
    from tongdory2.catalog.store import CatalogStore

    home_dir = tmp_path.resolve() / "home"
    sample = home_dir / "Desktop" / "회의록_1.txt"
    sample.parent.mkdir(parents=True)
    sample.write_text("가상 회의록", encoding="utf-8")
    dest = home_dir / "Documents" / "정리"
    (dest / "보고" / "회의록").mkdir(parents=True)
    (dest / "설계").mkdir(parents=True)

    app = create_app(Settings(TONGDORY_HOME=str(home_dir), TONGDORY_DATA_DIR=str(tmp_path / "app"), LINER_API_KEY=""))
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.post("/setup/structure", data={"paths": "보고/회의록\n설계"})
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    client.post("/setup/destination", data={"path": "~/Documents/정리"})

    setup_paths = ("/setup/structure", "/setup/sources", "/setup/destination")

    # 1) 3/3 확정 직후(분석 시작 전): 상황판 라벨은 '수정'이고 /setup/*는 잠금 없이 바로 수정 가능
    home_before = client.get("/").text
    assert re.findall(r'href="(/setup/[^"]+)">수정</a>', home_before) == list(setup_paths)
    assert re.findall(r'href="(/setup/[^"]+)">보기</a>', home_before) == []
    for path in setup_paths:
        page = client.get(path)
        assert page.status_code == 200
        assert 'id="readonly-bar"' not in page.text
        assert 'id="soft-past-bar"' not in page.text
        assert 'id="soft-past-backdrop"' not in page.text

    updated = client.post("/setup/structure", data={"paths": "보고/회의록\n설계\n아카이브"}, follow_redirects=False)
    assert updated.status_code == 303
    assert "아카이브" in client.get("/setup/structure").text

    # 2) 분석 실행 중(running=True): 상황판은 '보기', /setup/*는 잠기며 #open-unlock 버튼도 숨김
    app.state.t.job = {"running": True, "done": 0, "total": 1}
    try:
        home_running = client.get("/").text
        assert re.findall(r'href="(/setup/[^"]+)">보기</a>', home_running) == list(setup_paths)
        locked_running = client.get("/setup/structure")
        assert locked_running.status_code == 200
        assert 'id="readonly-bar"' in locked_running.text
        assert 'id="soft-past-bar"' in locked_running.text
        assert 'id="open-unlock"' not in locked_running.text
    finally:
        app.state.t.job = {}

    # 3) 분석 완료 후(analyzed=True): 상황판 라벨이 '보기'로 바뀌고 /setup/*에 읽기 전용 잠금·이전 단계 배너 표시
    client.get("/s2?new=1")
    prep_page = ""
    for _ in range(200):
        prep_page = client.get("/s2").text
        if 'name="prep_token"' in prep_page:
            break
        time.sleep(0.03)
    token = re.search(r'name="prep_token" value="([^"]+)"', prep_page).group(1)
    client.post("/s2/analyze", data={"prep_token": token})
    store = CatalogStore(app.state.t.db.conn)
    for _ in range(200):
        if store.latest() and store.latest()["status"] == "DONE":
            break
        time.sleep(0.03)

    home_after = client.get("/").text
    assert re.findall(r'href="(/setup/[^"]+)">보기</a>', home_after) == list(setup_paths)
    assert re.findall(r'href="(/setup/[^"]+)">수정</a>', home_after) == []
    for path in setup_paths:
        locked = client.get(path)
        assert locked.status_code == 200
        assert 'id="readonly-bar"' in locked.text
        assert 'id="soft-past-bar"' in locked.text
        assert '<dialog class="notice soft-past-bar"' in locked.text
        assert 'id="soft-past-backdrop"' not in locked.text
        assert 'id="open-unlock"' in locked.text


def test_structure_save_422_renders_notice_modal_and_preserves_input(tmp_path):
    home_dir = tmp_path.resolve() / "home"
    home_dir.mkdir(parents=True)
    app = create_app(Settings(TONGDORY_HOME=str(home_dir), TONGDORY_DATA_DIR=str(tmp_path / "app"), LINER_API_KEY=""))
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    bad_initial = client.post("/setup/structure", data={"paths": "../잘못된경로"})
    assert bad_initial.status_code == 422
    assert 'id="notice-modal"' in bad_initial.text
    assert re.search(r'<div class="notice warn" role="alert" data-notice-modal>[^<]+</div>', bad_initial.text)
    assert "../잘못된경로</textarea>" in bad_initial.text

    client.post("/setup/structure", data={"paths": "보고/회의록\n설계"})
    bad_edit = client.post("/setup/structure", data={"paths": "../수정오류"})
    assert bad_edit.status_code == 422
    assert 'data-notice-modal' in bad_edit.text
    textarea_match = re.search(r'<textarea id="structure-list"[^>]*>(.*?)</textarea>', bad_edit.text, re.S)
    assert textarea_match and textarea_match.group(1) == "../수정오류"

