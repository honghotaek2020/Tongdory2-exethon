"""2단계 파일명 Liner 빠른 분석(/s2/names/preview, /s2/names) 단위 테스트."""

from __future__ import annotations

import json
import os
import re
import time

import httpx
from fastapi.testclient import TestClient

from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.liner import client as liner
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import create_app


def _wait_for(client: TestClient, url: str, text: str) -> str:
    page = ""
    for _ in range(200):
        resp = client.get(url)
        page = resp.text
        if text in page:
            return page
        time.sleep(0.025)
    raise AssertionError(f"{text!r} not found in {url}: {page[:1000]}")


def test_name_liner_analysis_preview_approval_and_verdicts(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "home"
    source = home / "Desktop"
    destination = home / "Documents" / "저장"
    source.mkdir(parents=True)
    (destination / "참고" / "자료").mkdir(parents=True)

    modified = time.time() - 86400
    files: dict[str, bytes | str] = {
        "화면캡처_0001.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
        "고객미팅_메모.txt": "미팅 메모 요약\n본문전송금지표식",
        "알수없는문서.txt": "알 수 없는 문서\n본문전송금지표식",
        "애매한이미지.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR_2",
        "인증서.key": "가상의 민감 키 파일",
    }
    for name, content in files.items():
        path = source / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        os.utime(path, (modified, modified))

    requests: list[tuple[str, dict, str]] = []
    original_client = liner.LinerClient
    mock_answers: dict[str, list[dict]] = {"items": []}
    folder_answers: dict[str, list[dict]] = {"items": []}  # 1차-2 새 폴더 추천 응답

    def respond(request: httpx.Request) -> httpx.Response:
        raw = request.content.decode("utf-8")
        body = json.loads(raw)
        requests.append((request.url.path, body, raw))
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(
            200,
            json={
                "model": "gpt-6-luna-2026-10-01",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(folder_answers if "suggest_folders_by_names" in raw else mock_answers,
                                                  ensure_ascii=False),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 300, "completion_tokens": 90},
            },
        )

    monkeypatch.setattr(
        liner,
        "LinerClient",
        lambda *args, **kwargs: original_client(*args, transport=httpx.MockTransport(respond), **kwargs),
    )

    settings = Settings(
        TONGDORY_HOME=str(home),
        TONGDORY_DATA_DIR=str(tmp_path / "data"),
        LINER_API_KEY="test-only-key",
    )
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    client.post("/setup/structure", data={"paths": "보고/회의록\n참고/자료"})
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    client.post("/setup/destination", data={"path": "~/Documents/저장"})

    ready_page = _wait_for(client, "/s2?tab=check", 'name="prep_token"')
    prep_token = re.search(r'name="prep_token" value="([^"]+)"', ready_page).group(1)
    client.post("/s2/analyze", data={"prep_token": prep_token})

    check_page = _wait_for(client, "/s2?tab=check", "본문 내용으로 분류")
    assert "파일명으로 분류" in check_page
    assert 'formaction="/s2/names/preview"' in check_page
    assert 'formaction="/s2/documents/preview"' in check_page
    assert requests == []

    store = CatalogStore(app.state.t.db.conn)
    run_id = store.latest()["run_id"]
    before = {e["name"]: e for e in store.entries(run_id)}

    assert before["화면캡처_0001.png"]["verdict"] == "HOLD"
    assert before["고객미팅_메모.txt"]["verdict"] == "HOLD"
    assert before["인증서.key"]["verdict"] == "EXCLUDE"
    assert before["인증서.key"]["sensitivity"] == "L3"

    selected_ids = [
        before["화면캡처_0001.png"]["entry_id"],
        before["고객미팅_메모.txt"]["entry_id"],
        before["알수없는문서.txt"]["entry_id"],
        before["애매한이미지.png"]["entry_id"],
        before["인증서.key"]["entry_id"],
    ]
    preview_resp = client.post("/s2/names/preview", data={"run_id": run_id, "entry_ids": selected_ids})
    assert 'id="tree-panel"' in preview_resp.text  # 보낼 내용 확인 화면에도 폴더별 이동 계획 창 유지
    assert preview_resp.status_code == 200
    preview_html = preview_resp.text

    assert "파일명 4개를 Liner에 보낼까요?" in preview_html
    assert "본문은 보내지 않음" in preview_html
    assert "선택 5개 · 전송 대상 4개 · 제외 1개" in preview_html
    assert "화면캡처_0001.png" in preview_html
    assert "고객미팅_메모.txt" in preview_html
    assert "민감 파일은 파일명도 전송하지 않습니다." in preview_html
    assert "본문전송금지표식" not in preview_html
    assert requests == []

    approval = re.search(r'name="approval" value="([^"]+)"', preview_html).group(1)
    base_form = {"run_id": run_id, "entry_ids": selected_ids, "approval": approval}

    # reviewed 누락 또는 미동의 시 422 차단 및 외부 API 호출 0건
    assert client.post("/s2/names", data=base_form).status_code == 422
    assert client.post("/s2/names", data=base_form | {"reviewed": "no"}).status_code == 422
    assert requests == []

    spec = SpecStore(app.state.t.db.conn).current()
    node_by_path = {n["display_path"]: n["node_id"] for n in spec["body"]["nodes"]}
    mock_answers["items"] = [
        {
            "id": before["화면캡처_0001.png"]["entry_id"],
            "node_id": node_by_path["참고/자료"],
            "confidence": 0.91,
            "reason": "화면 캡처 참고 자료",
        },
        {
            "id": before["고객미팅_메모.txt"]["entry_id"],
            "node_id": node_by_path["보고/회의록"],
            "confidence": 0.88,
            "reason": "고객 미팅 회의록",
        },
        {
            "id": before["알수없는문서.txt"]["entry_id"],
            "node_id": "nonexistent.99.99",
            "confidence": 0.95,
            "reason": "존재하지 않는 노드",
        },
        {
            "id": before["애매한이미지.png"]["entry_id"],
            "node_id": node_by_path["보고/회의록"],
            "confidence": 0.50,
            "reason": "신뢰도 낮음",
        },
    ]

    folder_answers["items"] = [
        {"id": before["알수없는문서.txt"]["entry_id"], "name": "기타 문서", "reason": "종류 미상 문서"},
        {"id": before["애매한이미지.png"]["entry_id"], "name": "", "reason": "단서 없음"},
    ]

    submit_resp = client.post(
        "/s2/names",
        data=base_form | {"reviewed": "yes"},
        follow_redirects=False,
    )
    assert submit_resp.status_code == 303
    done_page = _wait_for(client, "/s2?result=1&tab=check", "파일명 Liner 분석 완료")
    # 팝업: 찾았는지 못 찾았는지 파일별로 보이고, 다시 들어오면 뜨지 않는다
    assert 'class="liner-result"' in done_page and "못 찾음" in done_page and "새 폴더" in done_page
    assert "파일명 Liner 분석 완료" not in client.get("/s2?result=1&tab=check").text

    # 1차-1 매칭 1회 + 1차-2 새 폴더 추천 1회 (1차-1에서 못 찾은 2개만)
    assert len(requests) == 2
    assert "suggest_folders_by_names" in requests[1][2]
    assert "알수없는문서.txt" in requests[1][2] and "애매한이미지.png" in requests[1][2]
    assert "고객미팅_메모.txt" not in requests[1][2] and "본문전송금지표식" not in requests[1][2]
    req_path, _req_body, req_raw = requests[0]
    assert req_path.endswith("/chat/completions")
    assert "본문전송금지표식" not in req_raw
    assert "인증서.key" not in req_raw
    assert "화면캡처_0001.png" in req_raw
    assert "고객미팅_메모.txt" in req_raw

    after = {e["name"]: e for e in store.entries(run_id)}

    img = after["화면캡처_0001.png"]
    assert img["verdict"] == "JOIN"
    assert img["node_path"] == "참고/자료"
    assert img["default_checked"] is True  # 이미지 L2는 전송 제한일 뿐 — 추천 폴더로 옮긴다
    assert img["reason_code"] == "LINER_NAME_MATCH"
    assert "1차-1 파일명 매칭" in img["badges"]

    doc = after["고객미팅_메모.txt"]
    assert doc["verdict"] == "SPEC_NODE"
    assert doc["node_path"] == "보고/회의록"
    assert doc["default_checked"] is True  # 추천한 기존 폴더는 바로 옮길 대상
    assert doc["reason_code"] == "LINER_NAME_MATCH"
    assert "1차-1 파일명 매칭" in doc["badges"]

    unknown = after["알수없는문서.txt"]
    assert unknown["verdict"] == "NEW_NODE"
    assert unknown["reason_code"] == "LINER_NAME_NEW"
    assert unknown["node_path"] == "기타문서"
    assert "1차-2 파일명 새 폴더" in unknown["badges"]

    low_conf = after["애매한이미지.png"]
    assert low_conf["verdict"] == "HOLD"
    assert low_conf["reason_code"] == "LINER_NAME_HOLD"

    sensitive = after["인증서.key"]
    assert sensitive["verdict"] == "EXCLUDE"
    assert sensitive["sensitivity"] == "L3"

    plan_html = client.get("/s2?result=1&tab=plan").text
    assert "Liner가 파일명으로 찾은 폴더" in plan_html  # 배지 설명(title)은 화면용 말로 표시

    # 새 폴더 추천은 승인 없이 정리 예정, 확인 필요 탭에는 직접 확인(Liner 수동 요청 막대)
    assert unknown["verdict"] == "NEW_NODE" and unknown["default_checked"] is True
    assert 'id="NEW_NODE"' in client.get("/s2?result=1&tab=plan").text
    check_html = client.get("/s2?result=1&tab=check").text
    assert 'id="HOLD"' in check_html and "파일명으로 분류" in check_html and "승인 필요" not in check_html
    # 승인 필요: 기본으로 옮기지 않는 파일만 — [모두 승인]하면 정리 예정으로
    store.conn.execute("UPDATE catalog_entries SET body=json_set(body,'$.default_checked',json('false'),'$.sensitivity','L2') WHERE run_id=? AND entry_id=?",
                       (run_id, unknown["entry_id"]))
    store.conn.commit()
    check_html = client.get("/s2?result=1&tab=check").text
    assert 'id="APPROVE"' in check_html and "모두 승인 (1개)" in check_html
    client.post("/s2/check", data={"run_id": run_id, "action": "approve_all"})
    assert unknown["entry_id"] in store.effective_selection(run_id, store.entries(run_id))
    assert 'id="APPROVE"' not in client.get("/s2?result=1&tab=check").text
    client.post("/s2/check", data={"run_id": run_id, "entry_id": unknown["entry_id"], "checked": "0"})
    store.conn.execute("UPDATE catalog_entries SET body=json_set(body,'$.sensitivity',?) WHERE run_id=? AND entry_id=?",
                       (unknown["sensitivity"], run_id, unknown["entry_id"]))
    store.conn.commit()

    # 파일명 분석으로도 미분류된 텍스트는 본문 분석으로 이어갈 수 있다.
    mixed = {"run_id": run_id, "entry_ids": [unknown["entry_id"], low_conf["entry_id"], sensitive["entry_id"]]}
    deep_preview = client.post("/s2/documents/preview", data=mixed)
    assert deep_preview.status_code == 200
    assert "선택 3개 · 전송 대상 1개 · 제외 2개" in deep_preview.text
    assert "본문 텍스트 추출을 지원하지 않는 파일" in deep_preview.text
    assert "민감 파일은 본문 분석 대상에서 제외" in deep_preview.text
    assert "알 수 없는 문서" in deep_preview.text
    assert len(requests) == 2  # 미리보기는 외부 호출을 하지 않는다.
    assert {e["name"]: e for e in store.entries(run_id)} == after  # 제외 사유는 원래 판정을 덮어쓰지 않는다.

    only_excluded = client.post("/s2/documents/preview", data={"run_id": run_id, "entry_ids": [low_conf["entry_id"]]})
    assert only_excluded.status_code == 200
    assert "선택 1개 · 전송 대상 0개 · 제외 1개" in only_excluded.text
    assert "보내고 분석 시작" not in only_excluded.text
    assert "보낼 문서가 없습니다" in only_excluded.text

    deep_requests = []
    def respond_deep(request):
        body = json.loads(request.content)
        deep_requests.append(request.content.decode())
        if request.url.path.endswith('/decisions'):
            question = next(iter(body['questions']))
            picked = 'current' if question == 'project' else 'n0'
            return httpx.Response(200,json={'answers':{question:{'choice':picked,'probabilities':{picked:0.99}}},'usage':{'cost':0.0001}})
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200,json={'data':[{'index':i,'embedding':[1,0]} for i in range(len(body['input']))]})
        if request.url.path.endswith('/rerank'):
            return httpx.Response(200,json={'results':[{'index':0,'relevance_score':0.99}]})
        raise AssertionError(request.url.path)
    monkeypatch.setattr(liner,'LinerClient',lambda *args,**kwargs: original_client(*args,transport=httpx.MockTransport(respond_deep),**kwargs))
    approval = re.search(r'name="approval" value="([^"]+)"',deep_preview.text).group(1)
    assert client.post('/s2/documents',data=mixed|{'approval':approval,'reviewed':'yes'},follow_redirects=False).status_code == 303
    _wait_for(client,'/s2?result=1&tab=check','Liner 분석 완료')
    assert deep_requests
    assert any('알 수 없는 문서' in payload for payload in deep_requests)
    assert all('애매한이미지.png' not in payload and '인증서.key' not in payload for payload in deep_requests)
    final = {e['name']:e for e in store.entries(run_id)}
    assert final['애매한이미지.png'] == low_conf
    assert final['인증서.key'] == sensitive


def test_name_hold_document_eligibility_keeps_security_gates():
    from tongdory2.catalog.pipeline import document_candidate
    for kind in ('text','pdf','docx','xlsx','pptx'):
        entry = dict(verdict='HOLD',reason_code='LINER_NAME_HOLD',sensitivity='L1',kind=kind)
        assert document_candidate(entry)
        assert document_candidate(entry|{'sensitivity':'L2'})
        assert not document_candidate(entry|{'sensitivity':'L3'})
        assert not document_candidate(entry|{'override':'some-folder'})
        assert not document_candidate(entry|{'verdict':'EXCLUDE'})
        assert not document_candidate(entry|{'reason_code':'PROJECT_CONFLICT'})
    for kind in (None,'image','binary'):
        assert not document_candidate(dict(verdict='HOLD',reason_code='LINER_NAME_HOLD',sensitivity='L1',kind=kind))
