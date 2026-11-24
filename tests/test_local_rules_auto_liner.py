"""로컬 폴더 대조 규칙(파일명 → 상위 폴더 이름)과 파일 분석 단계 Liner 자동 추천(최초 1회 동의)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tongdory2.catalog.local_folders import LocalFolderRules
from tongdory2.catalog.pipeline import NAME_CLASSIFY_BATCH, NAME_PARALLEL, Entry, classify_by_names
from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.liner import auto_consent
from tongdory2.liner import client as liner
from tongdory2.web.app import create_app


def _nodes(*paths: str) -> list[dict]:
    """display_path 목록 → 명세 노드 (상위 폴더도 DEST 노드로 만든다)."""
    out: dict[str, dict] = {}
    for path in paths:
        parts = path.split("/")
        for depth in range(1, len(parts) + 1):
            p = "/".join(parts[:depth])
            if p in out:
                continue
            label = re.sub(r"^\d+\.", "", parts[depth - 1])
            out[p] = {"node_id": f"n{len(out)}", "display_path": p, "label": label, "status": "ADOPTED",
                      "role": "DEST", "profile": "common", "description": "",
                      "parent_id": out["/".join(parts[:depth - 1])]["node_id"] if depth > 1 else None}
    return list(out.values())


SI = _nodes("01.착수/02.착수보고서", "02.분석/01.요구사항정의서", "03.설계/01.화면설계서", "03.설계/02.인터페이스설계서",
            "05.테스트/01.테스트시나리오", "05.테스트/02.테스트결과서", "08.보고/01.주간보고", "08.보고/02.회의록")


def _dest(rules: LocalFolderRules, rel: str) -> str | None:
    m = rules.match(rel)
    return m.node["display_path"] if m and m.node else None


def test_file_name_then_parent_folder():
    rules = LocalFolderRules(SI)
    assert _dest(rules, "x/eSIM_화면설계서_v1.pptx") == "03.설계/01.화면설계서"  # 폴더 이름 전체
    assert _dest(rules, "실증_결과서_v0.7.pptx") == "05.테스트/02.테스트결과서"  # 꼬리말(결과서)만
    # 파일명에 근거가 없으면 가까운 상위 폴더 → 먼 상위 폴더 순으로 (캡처 이미지·JSON)
    m = rules.match("관리자기능별화면결과서/20261109_captures/U07_key_details.png")
    assert m.node["display_path"] == "05.테스트/02.테스트결과서" and m.basis == "folder"
    assert m.folder == "관리자기능별화면결과서"
    assert rules.match("asn1/backup/der.txt") is None


def test_shared_head_goes_to_common_parent_and_generic_heads_to_their_folder():
    rules = LocalFolderRules(SI)
    # '설계서'는 두 하위 폴더에 같게 맞음 → 공통 상위 폴더
    assert _dest(rules, "design/20260716-audit-trail-설계서.md") == "03.설계"
    assert _dest(rules, "docs/script-chaining-design.md") == "03.설계"  # 영문 동의어
    # '보고서'는 '착수보고서'가 아니라 '08.보고' 몫 (꼬리말이 다른 폴더 이름)
    assert _dest(rules, "sms/미수신_문제점_보고서.md") == "08.보고"
    assert _dest(rules, "eUICC_Test_Suite_v14.pdf") == "05.테스트"


def test_weak_qualifier_defers_to_upper_folder_and_templates_skip_file_name():
    rules = LocalFolderRules(SI)
    # '캡쳐화면'의 '화면'(수식어 하나)은 약한 근거 → 더 위 폴더의 '결과서'로 정한다
    assert _dest(rules, "결과서작성문서/미수행기능_캡쳐화면/5352.png") == "05.테스트/02.테스트결과서"
    assert _dest(rules, "화면/1.png") == "03.설계/01.화면설계서"  # 더 위에서 못 정하면 약한 근거라도 쓴다
    # 양식 파일은 파일명으로 정하지 않지만 상위 폴더 이름으로는 정할 수 있다
    assert rules.match("회의록_양식.docx") is None
    assert _dest(rules, "회의록/회의록_양식.docx") == "08.보고/02.회의록"


def test_two_full_names_without_common_parent_stay_ambiguous():
    rules = LocalFolderRules(_nodes("보고/회의록", "분석/요구사항정의서"))
    m = rules.match("회의록_요구사항정의서.txt")
    assert m.node is None and {n["display_path"] for n in m.candidates} == {"보고/회의록", "분석/요구사항정의서"}


def _wait_for(client: TestClient, url: str, text: str) -> str:
    page = ""
    for _ in range(300):
        page = client.get(url).text
        if text in page:
            return page
        time.sleep(0.025)
    raise AssertionError(f"{text!r} not found: {page[:800]}")


def _setup(tmp_path, monkeypatch, key: str = "test-only-key"):
    home = tmp_path.resolve() / "home"
    source = home / "Desktop"
    (home / "Documents" / "저장").mkdir(parents=True)
    files = {"정리/주간회의록_1118.txt": "회의", "잡다/메모_고객.txt": "메모", "기타/알수없음.txt": "?",
             "인증서.key": "가상의 민감 키 파일"}
    modified = time.time() - 86400
    for rel, text in files.items():
        p = source / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        os.utime(p, (modified, modified))
    requests: list[str] = []
    original = liner.LinerClient

    def respond(request: httpx.Request) -> httpx.Response:
        raw = request.content.decode("utf-8")
        requests.append(raw)
        prompt = json.loads(raw)["messages"][1]["content"]
        meeting = re.search(r"- (\S+) \| 보고/회의록", prompt)
        items = []
        for eid, name in re.findall(r"- (e\d{4}) \| (\S+)", prompt):
            if "suggest_folders_by_names" in raw:
                items.append({"id": eid, "name": "계약", "reason": "새 종류"})
            elif "메모" in name:
                items.append({"id": eid, "node_id": meeting.group(1), "confidence": 0.9, "reason": "메모"})
            else:
                items.append({"id": eid, "node_id": "HOLD", "confidence": 0.2, "reason": "모름"})
        return httpx.Response(200, json={
            "model": "gpt-6-luna", "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": json.dumps({"items": items}, ensure_ascii=False)}}]})

    monkeypatch.setattr(liner, "LinerClient",
                        lambda *a, **kw: original(*a, transport=httpx.MockTransport(respond), **kw))
    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY=key)
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.post("/setup/structure", data={"paths": "보고/회의록\n참고/자료"})
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    client.post("/setup/destination", data={"path": "~/Documents/저장"})
    return app, client, settings, requests


def test_first_start_asks_once_then_auto_liner_runs_and_is_recorded(tmp_path, monkeypatch):
    app, client, settings, requests = _setup(tmp_path, monkeypatch)
    home = client.get("/").text
    assert 'id="start-analysis"' in home and 'id="liner-consent-first"' in home  # 버튼이 팝업을 연다
    assert client.get("/s2?new=1", follow_redirects=False).headers["location"] == "/?liner_consent=1"
    r = client.post("/settings/liner-auto", data={"choice": "on", "next": "/s2?new=1"}, follow_redirects=False)
    assert r.headers["location"] == "/s2?new=1" and auto_consent.load(settings.data_dir) is True
    client.get("/s2?new=1")
    notice = _wait_for(client, "/s2", 'name="prep_token"')
    assert "Liner 기존 폴더 매칭</span> 1개" in notice and "Liner 새 폴더 추천</span> 1개" in notice and "승인 필요" not in notice
    assert "내 PC 규칙</span> 1개" in notice
    sent = "\n".join(requests)
    assert "잡다/메모_고객.txt" in sent and "인증서" not in sent and "주간회의록" not in sent  # 로컬로 정한 파일·민감 파일은 안 보냄

    token = re.search(r'name="prep_token" value="([^"]+)"', notice).group(1)
    client.post("/s2/analyze", data={"prep_token": token})
    store = CatalogStore(app.state.t.db.conn)
    for _ in range(300):
        run = store.latest()
        if run and run["status"] == "DONE":
            break
        time.sleep(0.025)
    assert run["status"] == "DONE"
    by = {e["name"]: e for e in store.entries(run["run_id"])}
    assert by["주간회의록_1118.txt"]["reason_code"] == "LOCAL_FILE" and "로컬 파일명" in by["주간회의록_1118.txt"]["badges"]
    assert by["메모_고객.txt"]["reason_code"] == "LINER_NAME_MATCH" and by["메모_고객.txt"]["node_path"] == "보고/회의록"
    assert by["알수없음.txt"]["verdict"] == "NEW_NODE" and by["알수없음.txt"]["reason_code"] == "LINER_NAME_NEW"
    calls = app.state.t.db.conn.execute("SELECT COUNT(*) FROM api_calls WHERE run_id=?", (run["run_id"],)).fetchone()[0]
    assert calls == len(requests) == 2 and run["cost_usd"] > 0  # 자동 호출 기록이 이 묶음에 붙는다

    # 다시 시작해도 묻지 않는다
    assert 'id="liner-consent-first"' not in client.get("/").text


def test_decline_skips_liner_and_checkbox_reenables_with_popup(tmp_path, monkeypatch):
    app, client, settings, requests = _setup(tmp_path, monkeypatch)
    client.post("/settings/liner-auto", data={"choice": "off", "next": "/s2?new=1"})
    client.get("/s2?new=1")
    notice = _wait_for(client, "/s2", 'name="prep_token"')
    assert requests == [] and "Liner 기존 폴더" not in notice
    home = client.get("/").text
    assert 'id="liner-auto"' in home and 'id="liner-consent-again"' in home  # 언체크 상태 → 켜면 안내 팝업
    assert 'id="liner-consent-first"' not in home
    client.post("/settings/liner-auto", data={"choice": "on", "next": "/"})
    home = client.get("/").text
    assert re.search(r'id="liner-auto" checked', home) and 'id="liner-consent-again"' not in home


def test_agree_without_key_guides_to_key_page_and_runs_local_only(tmp_path, monkeypatch):
    app, client, settings, requests = _setup(tmp_path, monkeypatch, key="")
    r = client.post("/settings/liner-auto", data={"choice": "on", "next": "/s2?new=1"}, follow_redirects=False)
    assert r.headers["location"].startswith("/settings/liner?return_to=")
    assert "API 키를 등록하면" in client.get(r.headers["location"]).text
    client.get("/s2?new=1")  # [이전 화면으로 돌아가기] — 키 없이 이번 분석
    notice = _wait_for(client, "/s2", 'name="prep_token"')
    assert "API 키가 없어 내 PC 규칙만 사용했습니다" in notice and requests == []
    assert "API 키 등록 필요" in client.get("/").text


def test_truncated_reply_is_split_and_retried_without_losing_other_chunks():
    """응답이 잘려 JSON이 깨지면(2026-11-24 실제 오류) 반으로 나눠 다시 묻고, 끝까지 실패한 파일만 '분석 못함'으로."""
    from tongdory2.catalog.pipeline import _ask_split, _mark_failed

    entries = [Entry(entry_id=f"e{i}", path="", display="", name=f"f{i}.txt", source="", root_id="r") for i in range(8)]
    calls = []

    async def call(part):
        calls.append(len(part))
        if len(part) > 2 or any(e.entry_id == "e7" for e in part):  # 큰 묶음은 잘리고, e7이 들면 늘 실패
            raise liner.LinerError("chat content is not valid JSON", status=200, code="invalid_json")
        return {"items": [{"id": e.entry_id} for e in part]}

    items, failed = asyncio.run(_ask_split(entries, call, asyncio.Semaphore(1), asyncio.Event()))
    assert sorted(i["id"] for i in items) == [f"e{i}" for i in range(7)]
    assert [e.entry_id for e in failed] == ["e7"]
    _mark_failed(failed)
    assert failed[0].reason_code == "NOT_ANALYZED" and calls[0] == 8


@pytest.mark.parametrize("status", [401, 402, 403, 429])
def test_request_failure_does_not_split_name_batches(tmp_path, status):
    n_batches = NAME_PARALLEL + 2
    entries = [Entry(entry_id=f"e{i}", path="", display="", name=f"f{i}.txt", source="", root_id="r", verdict="PENDING")
               for i in range(NAME_CLASSIFY_BATCH * n_batches)]
    spec = {"body": {"nodes": _nodes("설계")}}
    calls = []

    class Client:
        async def chat_json(self, messages, _schema, task, **kwargs):
            calls.append((task, messages[1]["content"].count(" | f")))
            await asyncio.sleep(0)  # 첫 응답이 돌아오기 전에 동시 요청 한도를 채운다
            raise liner.LinerError("mock request failure", status=status)

    progress = {}
    asyncio.run(classify_by_names(entries, spec, tmp_path, Client(), progress))
    assert calls == [("classify_by_names", NAME_CLASSIFY_BATCH)] * (n_batches if status == 429 else NAME_PARALLEL)
    assert all(e.verdict == "HOLD" and e.reason_code == "NOT_ANALYZED" for e in entries)
    assert progress["done"] == len(entries)


def test_new_folder_proposals_reuse_names_only_within_the_same_parent():
    from tongdory2.catalog.pipeline import _place_new_folder

    nodes = _nodes("설계", "운영")
    design, operations = (n["node_id"] for n in nodes)
    used_paths = set()
    entries = [Entry(entry_id=f"e{i}", path="", display="", name=f"f{i}.txt", source="", root_id="r")
               for i in range(4)]
    for entry, parent in zip(entries, (design, operations, design, None)):
        _place_new_folder(entry, "참고자료", parent, nodes, used_paths)

    assert [entry.node_path for entry in entries] == ["설계/01.참고자료", "운영/01.참고자료", "설계/01.참고자료", "참고자료"]


def test_progress_page_shows_overall_steps(tmp_path, monkeypatch):
    app, client, settings, requests = _setup(tmp_path, monkeypatch)
    auto_consent.save(settings.data_dir, True)
    t = app.state.t
    t.prep_job = {"running": True, "stage": "1차-1 파일명 매칭", "done": 10, "total": 20, "batch_number": 1,
                  "started": time.time()}
    html = client.get("/s2").text
    assert 'aria-current="step">Liner 추천' in html and "전체 56%" in html  # (2 + (0 + 0.5) / 2) / 4, 버림
    assert html.count("<progress") - html.count("<progress aria-label=\"처리 중\"") == 1  # 막대는 전체 진행률 하나만
    auto_consent.save(settings.data_dir, False)
    t.prep_job.update(stage="파일 종류 살펴보기", done=300, total=0)
    html = client.get("/s2").text
    # 파일 종류 살펴보기는 따로 0→100%: 맨 위 폴더를 지난 비율
    t.prep_job["frac"] = 0.4
    html = client.get("/s2").text
    assert "(사용 안 함)" in html and "파일 종류 살펴보기 40%" in html and html.count("<progress") - html.count("<progress aria-label=\"처리 중\"") == 1
    t.prep_job["running"] = False
