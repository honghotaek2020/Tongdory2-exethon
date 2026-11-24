"""새 설정 순서와 분석·이동 결과의 설정 일치 조건을 검증한다."""

import io
import json
import os
import re
import threading
import time

import httpx
from docx import Document
from fastapi.testclient import TestClient
from openpyxl import Workbook

from tongdory2.catalog import pipeline
from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.fsx.roots import Role
from tongdory2.liner import client as liner
from tongdory2.plan.executor import list_batches
from tongdory2.spec.store import SpecStore
from tongdory2.spec.structure_input import document_paths
from tongdory2.web.app import create_app
from tongdory2.web.setup_state import scope_key


def test_structure_document_formats():
    workbook = Workbook()
    workbook.active.append(["단계", "산출물", "필수"])
    workbook.active.append(["보고", "회의록", "필수"])
    data = io.BytesIO()
    workbook.save(data)
    assert document_paths("정의서.xlsx", data.getvalue()) == [("보고/회의록", True)]

    doc = Document()
    table = doc.add_table(rows=2, cols=1)
    table.cell(0, 0).text = "폴더 경로"
    table.cell(1, 0).text = "설계/화면"
    data = io.BytesIO()
    doc.save(data)
    assert document_paths("정의서.docx", data.getvalue()) == [("설계/화면", False)]


def test_setup_analyze_plan_execute_and_undo(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "home"
    reference = home / "Documents" / "기존구조"
    destination = home / "Documents" / "저장"
    desktop = home / "Desktop"
    downloads = home / "Downloads"
    for folder in (reference / "보고" / "회의록", destination / "tmp", desktop, downloads):
        folder.mkdir(parents=True)
    (reference / "build").mkdir()
    (reference / "심볼릭").symlink_to(downloads)
    existing_version = destination / "tmp" / "회의록_v1.txt"
    existing_version.write_text("저장 위치 안에 이미 있던 자료")
    (existing_version.parent / "회의록_v9.txt").symlink_to(existing_version)
    source = desktop / "회의록_v2.txt"
    source.write_text("가상 회의록")
    modified = time.time() - 86400
    os.utime(source, (modified, modified))
    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(home.parent / "data"), LINER_API_KEY="")
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

    start = client.get("/").text
    assert start.index("분류 희망 폴더 목록") < start.index("정리 대상 폴더") < start.index("저장 폴더")
    assert "과거 과제 참고" not in start
    assert 'aria-controls="liner-history"' in start and "API 호출 기록이 없습니다" in start
    assert client.get("/setup/destination", follow_redirects=False).headers["location"] == "/setup/structure"
    assert client.get("/setup/browse", params={"path": "~/Documents"}).json()["children"]
    preview = client.post("/setup/structure/preview", data={"reference": "~/Documents/기존구조"})
    assert "보고/회의록" in preview.text and "심볼릭" not in preview.text and "build" not in preview.text
    assert 'id="reference" type="hidden" name="reference" value="~/Documents/기존구조"' in preview.text
    assert "번호 없는 폴더 2개" in preview.text
    assert 'id="setup-import-title"><span class="setup-section-number">1</span> 분류 희망 폴더 목록 가져오기' in preview.text
    assert 'id="setup-list-title"><span class="setup-section-number">2</span> 분류 희망 폴더 목록 편집' in preview.text
    assert 'data-tree-path="보고/회의록"' in preview.text and "폴더 2개를 가져왔습니다." in preview.text
    assert SpecStore(client.app.state.t.db.conn).current() is None
    document = client.post("/setup/structure/preview", files={"document": ("정의.txt", "보고/회의록".encode())})
    assert "분류 희망 폴더 목록 편집" in document.text
    assert "정의.txt" in document.text and 'data-tree-path="보고/회의록"' in document.text
    assert SpecStore(client.app.state.t.db.conn).current() is None
    assert client.post("/setup/structure", data={"paths": "../밖"}).status_code == 422

    def set_structure():
        return client.post("/setup/structure", data={"paths": "보고/회의록", "reference": "~/Documents/기존구조"},
                           follow_redirects=False)

    assert set_structure().headers["location"] == "/setup/sources"
    assert 'id="reference" type="hidden" name="reference" value="~/Documents/기존구조"' in client.get("/setup/structure").text
    assert not (destination / "보고").exists()
    assert client.get("/setup/destination", follow_redirects=False).headers["location"] == "/setup/sources"
    assert client.post("/setup/sources", data={"paths": "~/Desktop"}, follow_redirects=False).headers["location"] == "/setup/destination"
    assert client.post("/setup/destination", data={"path": "~/Desktop"}).status_code == 422
    assert client.post("/setup/destination", data={"path": "~/Documents/저장"}, follow_redirects=False).headers["location"] == "/"
    assert "이어서 할 수 있는 일" not in client.get("/").text

    def wait_for(pattern):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            page = client.get("/s2").text
            match = re.search(pattern, page)
            if match:
                return match.group(1)
            time.sleep(0.02)
        raise AssertionError(page[:1000])

    token_pattern = r'name="prep_token" value="([^"]+)"'
    # 최초 [파일 분석 시작]은 Liner 자동 추천 동의를 먼저 묻는다 → 이 테스트는 '동의 안 함'(로컬 규칙만)
    assert client.get("/s2?new=1", follow_redirects=False).headers["location"] == "/?liner_consent=1"
    assert 'id="liner-consent-first"' in client.get("/?liner_consent=1").text
    assert client.post("/settings/liner-auto", data={"choice": "off", "next": "/s2?new=1"},
                       follow_redirects=False).headers["location"] == "/s2?new=1"
    client.get("/s2?new=1", follow_redirects=False)
    old_token = wait_for(token_pattern)
    set_structure()
    assert client.post("/s2/analyze", data={"prep_token": old_token}, follow_redirects=False).headers["location"] == "/s2?new=1"

    def analyze_and_plan():
        client.get("/s2?new=1", follow_redirects=False)
        token = wait_for(token_pattern)
        client.post("/s2/analyze", data={"prep_token": token}, follow_redirects=False)
        run_id = wait_for(r'name="run_id" value="([^"]+)"')
        entry_id = wait_for(r'class="pick" data-id="([^"]+)"')
        response = client.post("/s3/plan", data={"run_id": run_id, "sel": entry_id}, follow_redirects=False)
        assert response.headers["location"].startswith("/s3/plan/")
        return run_id, response.headers["location"].rsplit("/", 1)[-1]

    run_id, stale_plan = analyze_and_plan()
    # 키가 없으면 Liner 버튼 대신 키 등록 안내를 보여 주고, 저장 뒤 검토 화면으로 돌아온다.
    review = client.get("/s2").text
    assert "파일명으로 분류" not in review
    review_url = re.search(r'name="return_to" value="(/s2[^"]*)"', review).group(1).replace("&amp;", "&")
    key_page = client.get("/settings/liner", params={"return_to": review_url}).text
    back = re.search(r'name="return_to" value="([^"]+)"', key_page).group(1).replace("&amp;", "&")
    assert back.startswith("/s2")
    from tongdory2.web.routes import app_settings

    async def key_ok(key, base_url):
        return "ok", 1.0
    monkeypatch.setattr(app_settings, "verify_key", key_ok)  # 실제 Liner에 묻지 않는다
    saved = client.post("/settings/liner", data={"action": "save", "key": "lnr-flow-key-1234", "return_to": back},
                        follow_redirects=False)
    assert saved.headers["location"] == back
    assert "liner-key-cta" not in client.get(back).text
    client.post("/settings/liner", data={"action": "delete"})
    assert client.get("/s3/plan/" + stale_plan).status_code == 200
    assert "비슷한 이전 버전 파일 1개" in client.get("/s3/plan/" + stale_plan).text
    assert "회의록_v1.txt" not in client.get("/s2").text
    client.post("/setup/sources", data={"paths": "~/Desktop\n~/Downloads"}, follow_redirects=False)
    assert client.post("/s3/plan", data={"run_id": run_id}, follow_redirects=False).headers["location"] == "/s2"
    assert client.post("/s3/execute/" + stale_plan, data={"confirm": "yes", "extra_confirm": "yes"}, follow_redirects=False).headers["location"] == "/s2"
    assert source.exists()

    _, stale_plan = analyze_and_plan()
    alternate = destination.parent / "임시저장"
    alternate.mkdir()
    client.post("/setup/destination", data={"path": str(alternate)})
    assert client.post("/s3/execute/" + stale_plan, data={"confirm": "yes", "extra_confirm": "yes"}, follow_redirects=False).headers["location"] == "/s2"
    assert source.exists() and not any(alternate.iterdir())
    client.post("/setup/destination", data={"path": str(destination)})

    _, stale_plan = analyze_and_plan()
    client.post("/setup/structure", data={"paths": "보고/회의록\n설계"})
    blocked_plan = client.get("/s3/plan/" + stale_plan).text
    assert "이 계획은 실행할 수 없습니다" in blocked_plan
    assert f'action="/s3/execute/{stale_plan}"' not in blocked_plan
    assert client.post("/s3/execute/" + stale_plan, data={"confirm": "yes", "extra_confirm": "yes"}, follow_redirects=False).headers["location"] == "/s2"
    assert source.exists() and existing_version.exists()
    assert not list_batches(client.app.state.t.db.conn)

    another = downloads / "가상기록.txt"
    another.write_text("미조사 파일")
    os.utime(another, (modified - 1, modified - 1))
    monkeypatch.setattr(pipeline, "MAX_ITEMS", 1)
    _, plan = analyze_and_plan()
    blocked = client.post("/s3/execute/" + plan, follow_redirects=False)
    assert blocked.headers["location"] == "/s3/plan/" + plan
    assert source.exists()
    assert client.post("/s3/execute/" + plan, data={"confirm": "yes", "extra_confirm": "yes"}, follow_redirects=False).headers["location"] == "/"
    moved = destination / "보고" / "회의록" / source.name
    assert moved.is_file() and not source.exists()
    assert existing_version.read_text() == "저장 위치 안에 이미 있던 자료"
    executed_home = client.get("/").text
    assert "분류 범위 미완료" in executed_home
    # 실행 뒤 상황판 오른쪽 목록은 분류 예정이 아니라 실제로 옮긴 결과를 보여 준다.
    panel = re.search(r'(?s)<aside class="tree-aside".*?</aside>', executed_home).group(0)
    assert 'data-ctx="result" data-count-label="이동 완료"' in panel and ">정리한 폴더<" in panel
    assert "이동할 파일" not in panel
    assert re.search(r'data-tree-path="보고/회의록"[^>]*data-changed="1"', panel)
    assert another.is_file()
    batch = re.search(r"/s3/undo/([^\"]+)", client.get("/s3").text).group(1)
    client.post("/s3/undo/" + batch, data={"confirm": "yes", "extra_confirm": "yes"})
    assert source.is_file() and not moved.exists()


def test_document_review_sends_only_approved_unchanged_documents(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "home"
    source = home / "Desktop"
    source.mkdir(parents=True)
    destination = home / "Documents" / "저장"
    destination.mkdir(parents=True)
    paths = {
        "회의록_20261118.txt": "로컬에서 분류하는 회의록",
        "GSMA_자료.txt": "분류가 필요한 자료\n연락처 example@example.invalid",
        "내부계약서.txt": "계약 요약\n본문전송금지표식",
        "미선택.txt": "미선택문서전송금지표식",
        "서버.key": "가상의 민감 파일",
        "회의록_요구사항정의서.txt": "서로 다른 폴더 두 곳과 일치하는 문서",
        "수정할파일.txt": "확인 후 바뀔 문서",
    }
    modified = time.time() - 86400
    for name, content in paths.items():
        path = source / name
        path.write_text(content)
        os.utime(path, (modified, modified))

    requests = []
    started, release = threading.Event(), threading.Event()
    original_client = liner.LinerClient

    def respond(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        requests.append((endpoint, request.content.decode()))
        if endpoint == "decisions":
            question = next(iter(body["questions"]))
            if question == "project":
                started.set()
                assert release.wait(4)
            picked = "current" if question == "project" else "n0"
            return httpx.Response(200, json={"answers": {question: {"choice": picked, "probabilities": {picked: 0.99}}},
                                             "usage": {"cost": 0.0001}})
        if endpoint == "embeddings":
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [1, 0]} for i in range(len(body["input"]))]})
        if endpoint == "rerank":
            index = next(i for i, option in enumerate(body["documents"]) if option.startswith("회의록:"))
            return httpx.Response(200, json={"results": [{"index": index, "relevance_score": 0.99}]})
        raise AssertionError(f"예상하지 않은 Liner 요청 {endpoint}")

    monkeypatch.setattr(liner, "LinerClient",
                        lambda *args, **kwargs: original_client(*args, transport=httpx.MockTransport(respond), **kwargs))
    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY="test-only-key")
    base = "http://127.0.0.1:8765"
    client = TestClient(create_app(settings), base_url=base)
    client.post("/setup/structure", data={"paths": "보고/회의록\n분석/요구사항정의서"})
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    client.post("/setup/destination", data={"path": "~/Documents/저장"})
    client.post("/settings/liner-auto", data={"choice": "off", "next": "/"})  # 자동 추천 거절 (수동 요청만)

    def wait_for(client, label):
        for _ in range(200):
            page = client.get("/s2?tab=check")
            if label in page.text:
                return page.text
            time.sleep(0.025)
        raise AssertionError(page.text[:1000])

    ready = wait_for(client, 'name="prep_token"')
    token = re.search(r'name="prep_token" value="([^"]+)"', ready).group(1)
    client.post("/s2/analyze", data={"prep_token": token})
    wait_for(client, "본문 내용으로 분류")
    assert requests == []
    local_home = client.get("/").text
    assert "내 PC 분류 7개 · Liner 분석 대상 0개" in local_home
    assert "외부 호출 0회 · 응답 캐시 재사용 0회 · 비용(추정값 포함) $0" in local_home
    assert "분석할 문서 선택" not in local_home

    # 확인 목록은 준비 작업의 메모리 없이도 디스크에서 다시 열 수 있다.
    app = create_app(settings)
    client = TestClient(app, base_url=base)
    store = CatalogStore(app.state.t.db.conn)
    run_id = store.latest()["run_id"]

    def entries():
        return {entry["name"]: entry for entry in store.entries(run_id)}

    before = entries()
    assert before["회의록_20261118.txt"]["verdict"] == "SPEC_NODE"
    assert before["서버.key"]["verdict"] == "EXCLUDE"
    assert before["회의록_요구사항정의서.txt"]["reason_code"] == "LOCAL_AMBIGUOUS"

    client.post("/s2/documents/preview", data={"run_id": run_id, "entry_ids": [before["서버.key"]["entry_id"]]})
    assert requests == []

    def preview(fields):
        page = client.post("/s2/documents/preview", data=fields).text
        token = re.search(r'name="approval" value="([^"]+)"', page).group(1)
        submit = re.search(r'<button [^>]*type="submit" name="reviewed" value="([^"]+)">보내고 분석 시작</button>', page)
        assert submit and submit.group(1) == "yes"
        return page, fields | {"approval": token, "reviewed": submit.group(1)}

    changing_selection = {"run_id": run_id, "entry_ids": [before["수정할파일.txt"]["entry_id"]]}
    _, formerly_approved = preview(changing_selection)
    changed = source / "수정할파일.txt"
    st = changed.stat()
    changed.write_text("전송 없이 보류해야 할 수정 문서")
    os.utime(changed, ns=(st.st_atime_ns, st.st_mtime_ns))
    # 완료 안내는 한 번만 뜬다 — 전송 뒤 돌아온 화면에서 이미 보였을 수 있다
    if "전송 내용이 바뀌었거나" not in client.post("/s2/documents", data=formerly_approved).text:
        wait_for(client, "전송 내용이 바뀌었거나")
    assert requests == []

    selection = {"run_id": run_id, "entry_ids": [before[n]["entry_id"]
                for n in ("GSMA_자료.txt", "내부계약서.txt", "수정할파일.txt")]}
    assert client.post("/s2/documents", data=selection).status_code == 422
    preview_page, approval = preview(selection)
    assert "선택 3개 · 전송 대상 2개" in preview_page
    assert "계약 요약" in preview_page and "본문전송금지표식" not in preview_page
    assert "example@example.invalid" not in preview_page and not requests
    assert 'type="checkbox" name="reviewed"' not in preview_page
    assert "개인정보를 *** 처리한 본문을 보내는 데 동의합니다" not in preview_page
    unchecked = approval.copy()
    unchecked.pop("reviewed")
    assert client.post("/s2/documents", data=unchecked).status_code == 422
    assert client.post("/s2/documents", data=approval | {"reviewed": "no"}).status_code == 422
    assert not requests
    try:
        client.post("/s2/documents", data=approval)
        assert started.wait(3)
        blocked = client.post("/setup/structure", data={"paths": "추가폴더"})
        assert blocked.status_code == 422 and "분석이 진행 중" in blocked.text
    finally:
        release.set()
    wait_for(client, "파일이 변경됐습니다")
    after = entries()
    assert set(endpoint for endpoint, _ in requests) == {"decisions", "embeddings", "rerank"}
    payloads = "\n".join(body for _, body in requests)
    assert "GSMA_자료.txt" in payloads and "내부계약서.txt" in payloads and "계약 요약" in payloads
    assert all(value not in payloads for value in ("본문전송금지표식", "미선택문서전송금지표식", "수정할파일.txt",
                                                   "서버.key", "회의록_20261118.txt", "example@example.invalid"))
    assert after["GSMA_자료.txt"]["node_path"] == "보고/회의록" and after["GSMA_자료.txt"]["default_checked"]
    assert after["미선택.txt"]["verdict"] == "HOLD"  # 방안 B: 기준 폴더에 맞지 않은 파일은 HOLD 유지
    assert after["수정할파일.txt"]["reason_code"] == "DOCUMENT_CHANGED"
    assert all(not entry["payload"] for entry in after.values())
    assert not any(destination.iterdir())
    assert store.latest()["cost_usd"] > 0
    analyzed_home = client.get("/").text
    assert "Liner 분석 대상 2개" in analyzed_home
    assert "외부 호출 7회 · 응답 캐시 재사용 0회 · 비용(추정값 포함) $0.0004" in analyzed_home
    assert "비용 미기록 호출 3회" in analyzed_home
    assert "<td>1개 → 3개</td>" not in analyzed_home
    assert "비용은 Liner 호출에 대해 발생합니다." in analyzed_home
    assert "2개 / 선택 3개" in analyzed_home
    assert "Decisions · 분류 판정" in analyzed_home and "외부 호출" in analyzed_home
    assert "본문전송금지표식" not in analyzed_home and "GSMA_자료.txt" not in analyzed_home
    plan_tab = client.get("/s2?tab=plan").text
    # Liner가 기존 폴더를 찾은 제안은 기본 선택 — 다시 확인하라고 요구하지 않는다
    # Liner가 기존 폴더를 찾은 제안은 기본 선택 — 민감 가능(L2) 계약서 1개만 직접 확인
    assert "Liner 제안 1개는 확인 후 체크하세요" in plan_tab
    assert after["내부계약서.txt"]["sensitivity"] == "L2" and not after["내부계약서.txt"]["default_checked"]

    count = len(requests)
    client.post("/s2/documents", data=approval)
    assert len(requests) == count
    spec = SpecStore(app.state.t.db.conn).current()
    node = next(n["node_id"] for n in spec["body"]["nodes"] if n["display_path"] == "보고/회의록")
    client.post("/s2/assign", data={"run_id": run_id, "entry_ids": [before["미선택.txt"]["entry_id"]], "target": node})
    assert entries()["미선택.txt"]["override"] == node
    assert (source / "미선택.txt").exists()

    def rerun(result_text):
        client.get("/s2?new=1")
        page = wait_for(client, 'name="prep_token"')
        token = re.search(r'name="prep_token" value="([^"]+)"', page).group(1)
        client.post("/s2/analyze", data={"prep_token": token})
        wait_for(client, result_text)
        return store.latest()["run_id"]

    # 서버 재시작 뒤 구조에 폴더를 추가해도 호환되는 제안을 유료 호출 없이 재사용한다.
    app = create_app(settings)
    client = TestClient(app, base_url=base)
    store = CatalogStore(app.state.t.db.conn)
    client.post("/setup/structure", data={"paths": "보고/회의록\n분석/요구사항정의서\n추가폴더"})
    run_id = rerun("이동할 폴더 제안 재사용 2개")
    cached = entries()
    assert cached["GSMA_자료.txt"]["node_path"] == "보고/회의록" and cached["GSMA_자료.txt"]["default_checked"]
    assert store.latest()["cost_usd"] == 0 and len(requests) == count
    assert "외부 호출 0회 · 응답 캐시 재사용 0회 · 이동할 폴더 제안 재사용 2개 · 비용(추정값 포함) $0" in client.get("/").text
    assert "1개 → 3개" not in client.get("/").text

    # 크기와 수정 시각을 유지해도 바뀐 원본에는 이전 결과를 쓰지 않는다.
    revised = source / "GSMA_자료.txt"
    old = revised.stat()
    revised.write_bytes(b"x" * old.st_size)
    os.utime(revised, ns=(old.st_atime_ns, old.st_mtime_ns))
    run_id = rerun("이동할 폴더 제안 재사용 1개")
    assert entries()["GSMA_자료.txt"]["verdict"] in ("HOLD", "ETC")  # 재사용 안 함 → 낱개(기타 후보)
    assert entries()["내부계약서.txt"]["verdict"] == "SPEC_NODE"

    # 원본이 같아도 확정 구조에서 빠진 목적지는 제안하지 않는다.
    client.post("/setup/structure", data={"paths": "보고/다른목적지"})
    run_id = rerun("확인 필요")
    assert {entries()["GSMA_자료.txt"]["verdict"], entries()["내부계약서.txt"]["verdict"]} <= {"HOLD", "ETC"}
    assert len(requests) == count


def test_liner_popup_reads_previous_usage_and_cache_without_network(tmp_path):
    home = tmp_path.resolve() / "home"
    (home / "Desktop").mkdir(parents=True)
    (home / "Documents" / "정리").mkdir(parents=True)
    setting = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY="")
    app = create_app(setting)
    conn = app.state.t.db.conn
    store = CatalogStore(conn)
    older = store.start(1, {})
    store.finish(older, [], 0.0014, notice={"send_names": 5})
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.post("/setup/structure", data={"paths": "회의록"})
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    client.post("/setup/destination", data={"path": "~/Documents/정리"})
    spec = SpecStore(conn).current()
    current_notice = {"scope_key": scope_key(app.state.t.db.list_roots(), spec), "send_names": 1}
    run_id = store.start(spec["version"], current_notice)
    store.finish(run_id, [], 0, notice=current_notice)
    conn.executescript(liner.API_CALLS_DDL)
    with conn:
        conn.execute("INSERT INTO api_calls VALUES(?,?,?,?,?,?,?,?,?)",
                     ("cache1", run_id, "/decisions", None, None, 0, "CACHED", 0, "2026-11-18T00:00:00+00:00"))
        conn.execute("INSERT INTO api_calls VALUES(?,?,?,?,?,?,?,?,?)",
                     ("old", older, "/decisions", None, None, 0.0014, "REPORTED", 0, "2026-11-18T00:00:00+00:00"))
    page = client.get("/").text
    assert '<dialog id="liner-history"' in page and "최근 선택 문서 분석" in page
    assert "5개" in page and "$0.0014" in page
    assert "Decisions · 분류 판정" in page and "응답 캐시" in page
    assert "외부 호출 0회 · 응답 캐시 재사용 1회 · 비용(추정값 포함) $0" in page
    assert "외부 호출 0회 · 응답 캐시 재사용 1회 · 비용(추정값 포함) $0" in client.get("/s2").text


def test_setup_rejects_overlaps_and_browse_unsafe_paths(tmp_path):
    home = tmp_path.resolve() / "home"
    source = home / "Desktop"
    dest = home / "Documents" / "정리"
    app_data = home / "앱데이터"
    for folder in (source, dest / "안쪽", home / ".ssh"):
        folder.mkdir(parents=True)
    (home / "Documents" / "링크").symlink_to(source)
    (home / "Documents" / "문서.txt").write_text("파일은 찾아보기에 내보내지 않습니다.")
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(app_data), LINER_API_KEY=""))
    client = TestClient(app, base_url="http://127.0.0.1:8765", follow_redirects=False)
    result = client.get("/setup/browse", params={"path": "~/Documents"})
    assert result.status_code == 200
    assert [d["name"] for d in result.json()["children"]] == ["정리"]
    for path in ("~/Documents/../Desktop", "~/Documents/링크", "~/.ssh", str(app_data), "/System"):
        assert client.get("/setup/browse", params={"path": path}).status_code == 422

    client.post("/setup/structure", data={"paths": "회의록"})
    client.post("/setup/sources", data={"paths": str(source)})
    assert client.post("/setup/destination", data={"path": str(dest)}).status_code == 303
    roots = app.state.t.db.list_roots()
    for selected in (dest, dest.parent, dest / "안쪽"):
        response = client.post("/setup/sources", data={"paths": str(selected)})
        assert response.status_code == 422 and "기존 저장 폴더" in response.text
        assert app.state.t.db.list_roots() == roots
    assert {r.path for r in roots if r.role == Role.CURRENT} == {dest}
    assert client.post("/setup/destination", data={"path": str(source)}).status_code == 422


def test_public_standard_search_sends_only_approved_query(tmp_path, monkeypatch):
    calls = []
    api_type = liner.LinerClient

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"results": [
            {"title": "공개 규격", "url": "https://www.rfc-editor.org/rfc/rfc8446"},
            {"title": "잘못된 링크", "url": "javascript:example()"},
        ]})

    monkeypatch.setattr(liner, "LinerClient",
                        lambda *args, **kwargs: api_type(*args, transport=httpx.MockTransport(respond), **kwargs))
    settings = Settings(TONGDORY_HOME=str(tmp_path), TONGDORY_DATA_DIR=str(tmp_path / "data"),
                        LINER_API_KEY="fake-search-key")
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
    preview = client.get("/s2/standards", params={"identifier": "rfc8446"})
    assert "RFC 8446 official specification" in preview.text and not calls
    approval = re.search(r'name="approval" value="([^"]+)"', preview.text).group(1)
    for data in ({"identifier": "RFC 8446", "approval": "bad"}, {"identifier": "SGP.22", "approval": approval},
                 {"identifier": "RFC 8446 사내 문서", "approval": approval}):
        assert client.post("/s2/standards", data=data).status_code == 422
    assert not calls
    for _ in range(2):
        result = client.post("/s2/standards", data={"identifier": "RFC 8446", "approval": approval})
        assert result.status_code == 200 and "https://www.rfc-editor.org/rfc/rfc8446" in result.text
        assert "javascript:example()" not in result.text
    assert len(calls) == 1
    assert calls[0].url.path.endswith("/tools/search/web")
    assert calls[0].headers["x-api-key"] == "fake-search-key"
    assert json.loads(calls[0].content) == {"query": "RFC 8446 official specification", "max_results": 3,
                                            "include_knowledge": False}
