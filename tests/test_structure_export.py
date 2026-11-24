"""v3.0 폴더 목록 저장·재사용(1단계) 및 최종 결과 목록표 저장(4단계·인수인계) 검증."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.spec.store import SpecStore
from tongdory2.spec.structure_input import (
    compact_entries,
    document_paths,
    export_structure_json,
    export_structure_txt,
    extraction,
    parse_editor_text,
    parse_md_structure,
)
from tongdory2.web.app import create_app


def test_folder_reason_updates_legacy_terms_without_changing_document_names():
    from tongdory2.messages import folder_reason

    assert folder_reason("명세에 맞는 폴더 없음 — 새 폴더 '09조사' 제안") == "분류 희망 폴더 목록에서 맞는 곳을 찾지 못함 — 새 폴더 '09조사' 제안"
    assert folder_reason("급여명세서.xlsx · 파일명 일치") == "급여명세서.xlsx · 파일명 일치"


def test_normalize_compact_and_markdown_indent_rules():
    # 끝 슬래시·슬래시 주변 공백·[필수] 정규화 및 상위/하위 압축(중복 아닌 부모+자식은 허용)
    entries = parse_editor_text(
        "01.착수 / 01.과제수행계획서/ [필수]\n"
        "01.착수\n"
        "02.분석 [필수]\n"
        "02.분석/01.요구사항정의서\n"
    )
    compacted = compact_entries(entries)
    assert compacted == [
        ("01.착수/01.과제수행계획서", True),
        ("02.분석", True),  # [필수]가 붙은 상위 폴더는 유지
        ("02.분석/01.요구사항정의서", False),
    ]

    # TC-05: 공백·끝 슬래시 정규화 후 같은 경로 중복은 오류로 차단
    for dup_text in ("A/B\nA / B\n", "A/B\nA/B/\n", "A/B [필수]\nA/B\n"):
        with pytest.raises(ValueError, match="중복"):
            parse_editor_text(dup_text)
    with pytest.raises(ValueError, match="하위 폴더 경로"):
        extraction(parse_editor_text("보고//회의록\n"))

    # 2칸 들여쓰기 Markdown + BOM + 코드블록 + 인용구 무시
    md_2 = (
        "\ufeff# 프로젝트 폴더 목록\n"
        "> 참고 문구\n"
        "```\n"
        "- 코드블록안/무시항목\n"
        "```\n"
        "- 01.착수\n"
        "  - 01.과제수행계획서 [필수]\n"
        "  - 02.착수보고서\n"
        "- 02.분석 [필수]\n"
        "  - 01.요구사항정의서 [필수]\n"
        "  - 02.현행분석\n"
        "    - 01.외부연계현황\n"
    )
    assert parse_md_structure(md_2) == [
        ("01.착수/01.과제수행계획서", True),
        ("01.착수/02.착수보고서", False),
        ("02.분석", True),
        ("02.분석/01.요구사항정의서", True),
        ("02.분석/02.현행분석/01.외부연계현황", False),
    ]

    # 4칸 들여쓰기 Markdown 자동 인식
    md_4 = "- 01.설계\n    - 01.화면설계서 [필수]\n        - 모바일\n"
    assert parse_md_structure(md_4) == [
        ("01.설계/01.화면설계서", True),
        ("01.설계/01.화면설계서/모바일", False),
    ]

    # 중간 단계 건너뛴 들여쓰기 차단(줄 번호 안내 포함)
    with pytest.raises(ValueError, match="3번째 줄"):
        parse_md_structure("- 01.착수\n  - 01.계획\n      - 건너뜀\n")

    # 탭 들여쓰기 차단
    with pytest.raises(ValueError, match="탭"):
        parse_md_structure("- 01.착수\n\t- 01.계획\n")

    # 첫 줄부터 들여쓰기 차단
    with pytest.raises(ValueError, match="1번째 줄"):
        parse_md_structure("  - 01.계획\n")


def test_json_and_txt_roundtrip_and_validation():
    raw_entries = [
        ("01.착수/01.과제수행계획서", True),
        ("01.착수/02.착수보고서", False),
        ("02.분석/01.요구사항정의서", True),
    ]
    json_bytes = export_structure_json(raw_entries).encode("utf-8")
    assert document_paths("목록.json", json_bytes) == raw_entries

    txt_bytes = export_structure_txt(raw_entries).encode("utf-8")
    assert document_paths("목록.txt", txt_bytes) == raw_entries

    # 잘못된 format / version / 비어 있는 목록 / 잘못된 required 타입 차단
    for bad in (
        {"format": "other", "version": 1, "folders": [{"path": "보고", "required": False}]},
        {"format": "tongdory-folder-list", "version": 2, "folders": [{"path": "보고", "required": False}]},
        {"format": "tongdory-folder-list", "version": 1, "folders": []},
        {"format": "tongdory-folder-list", "version": 1, "folders": [{"path": "보고", "required": "yes"}]},
    ):
        with pytest.raises(ValueError):
            document_paths("bad.json", json.dumps(bad).encode("utf-8"))

    # 금지 경로 차단
    ex_entries = document_paths(
        "bad_path.json",
        json.dumps({"format": "tongdory-folder-list", "version": 1, "folders": [{"path": ".git/hooks", "required": False}]}).encode("utf-8"),
    )
    with pytest.raises(ValueError):
        extraction(ex_entries)


def test_step1_inline_required_and_export_mode1(tmp_path):
    home = tmp_path.resolve() / "home"
    export_dir = home / "Documents" / "내보내기"
    export_dir.mkdir(parents=True)
    git_dir = home / "Documents" / "repo" / ".git"
    git_dir.mkdir(parents=True)
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    symlink_dir = home / "Documents" / "링크폴더"
    symlink_dir.symlink_to(export_dir)

    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY="")
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    # 1) .md(2칸 들여쓰기) 파일 미리보기 → [필수] 표시 보존 + 완료 안내 + 트리 패널 표시
    md_bytes = "- 01.착수\n  - 01.과제수행계획서 [필수]\n  - 02.착수보고서\n".encode("utf-8")
    preview = client.post("/setup/structure/preview", files={"document": ("구조.md", md_bytes)})
    assert preview.status_code == 200
    assert "01.착수/01.과제수행계획서 [필수]" in preview.text
    assert 'data-tree-path="01.착수/01.과제수행계획서"' in preview.text
    assert "폴더 3개를 가져왔습니다." in preview.text

    # 2) 브라우저 다운로드 방식 (save_dir 비움) — JSON & TXT
    dl_json = client.post(
        "/setup/structure/export",
        data={"paths": "01.착수/01.과제수행계획서 [필수]\n01.착수/02.착수보고서", "fmt": "json", "filename": "내과제_기준"},
    )
    assert dl_json.status_code == 200
    assert dl_json.headers["x-content-type-options"] == "nosniff"
    assert "attachment;" in dl_json.headers["content-disposition"]
    assert ".json" in dl_json.headers["content-disposition"]
    parsed = dl_json.json()
    assert parsed == {
        "format": "tongdory-folder-list",
        "version": 1,
        "folders": [
            {"path": "01.착수/01.과제수행계획서", "required": True},
            {"path": "01.착수/02.착수보고서", "required": False},
        ],
    }

    dl_txt = client.post(
        "/setup/structure/export",
        data={"paths": "01.착수/01.과제수행계획서 [필수]\n01.착수/02.착수보고서", "fmt": "txt", "filename": "내과제_기준"},
    )
    assert dl_txt.status_code == 200
    assert dl_txt.text == "01.착수/01.과제수행계획서 [필수]\n01.착수/02.착수보고서\n"

    # 3) 방식 1: 선택한 폴더 직접 저장 + 덮어쓰기 금지(_1 생성)
    first = client.post(
        "/setup/structure/export",
        data={
            "paths": "01.착수/01.과제수행계획서 [필수]",
            "fmt": "json",
            "filename": "기준목록",
            "save_dir": "~/Documents/내보내기",
        },
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["ok"] is True
    assert first_body["filename"] == "기준목록.json"
    saved1 = export_dir / "기준목록.json"
    assert saved1.is_file()

    second = client.post(
        "/setup/structure/export",
        data={
            "paths": "01.착수/02.착수보고서",
            "fmt": "json",
            "filename": "기준목록",
            "save_dir": "~/Documents/내보내기",
        },
    )
    assert second.status_code == 200
    assert second.json()["filename"] == "기준목록_1.json"
    assert (export_dir / "기준목록_1.json").is_file()
    # 첫 번째 파일이 덮어씌워지지 않았는지 확인
    assert "01.과제수행계획서" in saved1.read_text(encoding="utf-8")

    # 4) 보안 경계 검증: 시스템·보안·Git·심볼릭 링크·교차 출처(CSRF) 차단
    for bad_dir in ("~/.ssh", str(tmp_path / "data"), str(git_dir), str(symlink_dir), "~/Documents/../.ssh", "/"):
        blocked = client.post(
            "/setup/structure/export",
            data={"paths": "보고/회의록", "fmt": "json", "filename": "차단", "save_dir": bad_dir},
        )
        assert blocked.status_code == 422, bad_dir

    csrf = client.post(
        "/setup/structure/export",
        data={"paths": "보고/회의록", "fmt": "json", "filename": "차단"},
        headers={"Origin": "http://evil.example"},
    )
    assert csrf.status_code == 403

    # 5) 인라인 [필수] 편집이 SpecStore에 정확히 반영되고, 제거 시 해제됨
    assert client.post(
        "/setup/structure",
        data={"paths": "01.착수/01.과제수행계획서 [필수]\n01.착수/02.착수보고서"},
        follow_redirects=False,
    ).status_code == 303
    spec = SpecStore(app.state.t.db.conn).current()
    req_map = {n["display_path"]: n["required_for_handover"] for n in spec["body"]["nodes"]}
    assert req_map["01.착수/01.과제수행계획서"] is True
    assert req_map["01.착수/02.착수보고서"] is False

    # [필수]를 지우고 다시 저장하면 required_for_handover도 False가 됨
    assert client.post(
        "/setup/structure",
        data={"paths": "01.착수/01.과제수행계획서\n01.착수/02.착수보고서"},
        follow_redirects=False,
    ).status_code == 303
    spec2 = SpecStore(app.state.t.db.conn).current()
    req_map2 = {n["display_path"]: n["required_for_handover"] for n in spec2["body"]["nodes"]}
    assert req_map2["01.착수/01.과제수행계획서"] is False


def test_step4_done_and_s4_final_catalog_l3_exclusion_and_step1_reuse(tmp_path):
    home = tmp_path.resolve() / "home"
    source = home / "Desktop"
    destination = home / "Documents" / "과제_QR결제"
    export_dir = home / "Documents" / "인수인계보관"
    for d in (source, destination / "01.착수" / "01.과제수행계획서", destination / "자동분류" / "세미나자료", export_dir):
        d.mkdir(parents=True)

    # 일반 파일, 초안 파일, 새로 만든 폴더 파일, 그리고 L3 민감 파일 2종(이름 기반 + 내용 기반)
    (destination / "01.착수" / "01.과제수행계획서" / "과제수행계획서_v1.0.txt").write_text("계획서 본문", encoding="utf-8")
    (destination / "01.착수" / "01.과제수행계획서" / "운영서버.key").write_text("dummy key", encoding="utf-8")
    (destination / "01.착수" / "01.과제수행계획서" / "일반이름이지만비밀.txt").write_text(
        "-----BEGIN PRIVATE KEY-----\nabcdef123456\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    (destination / "자동분류" / "세미나자료" / "QR동향_요약.txt").write_text("세미나 요약", encoding="utf-8")

    # 이동 테스트용 원본 파일 1건
    src_file = source / "회의록_20261120.txt"
    src_file.write_text("주간 회의록 내용", encoding="utf-8")
    old_t = time.time() - 86400
    os.utime(src_file, (old_t, old_t))

    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY="")
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    # 1~3단계 설정
    assert client.post(
        "/setup/structure",
        data={"paths": "01.착수/01.과제수행계획서 [필수]\n02.설계/01.화면설계서 [필수]\n08.보고/02.회의록"},
        follow_redirects=False,
    ).status_code == 303
    assert client.post("/setup/sources", data={"paths": "~/Desktop"}, follow_redirects=False).status_code == 303
    assert client.post("/setup/destination", data={"path": "~/Documents/과제_QR결제"}, follow_redirects=False).status_code == 303

    # 분석 -> 계획 -> 실행
    import re
    client.get("/s2?new=1")
    deadline = time.monotonic() + 5
    token = None
    while time.monotonic() < deadline:
        page = client.get("/s2").text
        m = re.search(r'name="prep_token" value="([^"]+)"', page)
        if m:
            token = m.group(1)
            break
        time.sleep(0.02)
    assert token
    client.post("/s2/analyze", data={"prep_token": token})
    run_id = entry_id = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        page = client.get("/s2").text
        rm = re.search(r'name="run_id" value="([^"]+)"', page)
        em = re.search(r'class="pick" data-id="([^"]+)"', page)
        if rm and em:
            run_id, entry_id = rm.group(1), em.group(1)
            break
        time.sleep(0.02)
    assert run_id and entry_id
    plan_res = client.post("/s3/plan", data={"run_id": run_id, "sel": entry_id}, follow_redirects=False)
    plan_id = plan_res.headers["location"].rsplit("/", 1)[-1]
    assert client.post(
        f"/s3/execute/{plan_id}",
        data={"confirm": "yes", "extra_confirm": "yes"},
        follow_redirects=False,
    ).status_code == 303

    # 실행 완료 배치 조회
    s3_list = client.get("/s3").text
    batch_id = re.search(r"/s3/undo/([^\"]+)", s3_list).group(1)

    # 1) 4단계 완료 화면(/s3?batch=...&done=1) UI 확인
    done_page = client.get(f"/s3?batch={batch_id}&done=1").text
    assert "파일 1개를 옮겼습니다" in done_page
    assert "파일 목록 저장" in done_page
    assert "폴더별 정리 결과" in done_page
    assert "이번 작업 상세 · 파일 이동 1개" in done_page
    assert "작업 내역 보기" in done_page
    assert "과제수행계획서_v1.0.txt" not in done_page
    assert "회의록_20261120.txt" in done_page
    assert "QR동향_요약.txt" not in done_page
    # 민감(L3) 파일 2종은 화면 목록표에도 절대 노출되지 않음
    assert "운영서버.key" not in done_page
    assert "일반이름이지만비밀.txt" not in done_page

    # 2) 인수인계 화면(/s4)에도 [최종 목록표 저장] 버튼이 있고 우측 트리 패널은 없음
    s4_page = client.get("/s4").text
    assert "data-open-final-catalog" in s4_page
    assert 'id="tree-panel"' not in s4_page

    # 3) 미리보기(MD / JSON / TXT) L3 차단 및 내용 검증
    md_preview = client.get(f"/s4/catalog/preview?fmt=md&batch={batch_id}")
    assert md_preview.status_code == 200
    assert "# 개발 자료 최종 목록표 (인수인계용)" in md_preview.text
    assert "과제수행계획서_v1.0.txt" in md_preview.text
    assert "회의록_20261120.txt" in md_preview.text
    assert "02.설계/01.화면설계서" in md_preview.text
    assert "운영서버.key" not in md_preview.text
    assert "일반이름이지만비밀.txt" not in md_preview.text
    assert "민감(L3) 파일 2개" in md_preview.text

    json_preview = client.get(f"/s4/catalog/preview?fmt=json&batch={batch_id}")
    assert json_preview.status_code == 200
    json_obj = json.loads(json_preview.text)
    assert json_obj["format"] == "tongdory-folder-list"
    assert json_obj["version"] == 1
    assert json_obj["exported_from"] == "final-result"
    by_path = {f["path"]: f for f in json_obj["folders"]}
    assert by_path["01.착수/01.과제수행계획서"] == {
        "path": "01.착수/01.과제수행계획서",
        "required": True,
        "file_count": 1,  # L3 2개 제외 → 일반 파일 1개만 집계
    }
    assert by_path["02.설계/01.화면설계서"]["required"] is True
    assert by_path["08.보고/02.회의록"]["file_count"] == 1
    assert by_path["자동분류/세미나자료"]["file_count"] == 1
    assert "운영서버.key" not in json_preview.text
    assert "일반이름이지만비밀.txt" not in json_preview.text

    txt_preview = client.get(f"/s4/catalog/preview?fmt=txt&batch={batch_id}")
    assert txt_preview.status_code == 200
    assert "01.착수/01.과제수행계획서 [필수]" in txt_preview.text
    assert "운영서버.key" not in txt_preview.text

    # 4) 방식 1: 선택한 폴더 직접 저장 + 중복 이름 _1 자동 분기
    exp_md = client.post(
        "/s4/catalog/export",
        data={"fmt": "md", "filename": "QR결제_인수인계", "save_dir": "~/Documents/인수인계보관", "batch": batch_id},
    )
    assert exp_md.status_code == 200
    assert exp_md.json()["filename"] == "QR결제_인수인계.md"
    saved_md = export_dir / "QR결제_인수인계.md"
    assert saved_md.is_file()
    md_disk = saved_md.read_text(encoding="utf-8")
    assert "과제수행계획서_v1.0.txt" in md_disk
    assert "운영서버.key" not in md_disk
    assert "일반이름이지만비밀.txt" not in md_disk

    exp_md2 = client.post(
        "/s4/catalog/export",
        data={"fmt": "md", "filename": "QR결제_인수인계", "save_dir": "~/Documents/인수인계보관", "batch": batch_id},
    )
    assert exp_md2.status_code == 200
    assert exp_md2.json()["filename"] == "QR결제_인수인계_1.md"

    # 5) 4단계에서 내보낸 최종 JSON을 다음 과제 1단계 [파일 선택]으로 그대로 재사용(100% 호환)
    exp_json = client.post(
        "/s4/catalog/export",
        data={"fmt": "json", "filename": "다음과제_기준", "batch": batch_id},
    )
    assert exp_json.status_code == 200
    assert "attachment;" in exp_json.headers["content-disposition"]
    reimport = client.post(
        "/setup/structure/preview",
        files={"document": ("다음과제_기준.json", exp_json.content)},
    )
    assert reimport.status_code == 200
    assert "01.착수/01.과제수행계획서 [필수]" in reimport.text
    assert "02.설계/01.화면설계서 [필수]" in reimport.text
    assert "자동분류/세미나자료" in reimport.text

    # 6) 되돌리기(Undo) 후 완료 화면 및 MD 미리보기에서 되돌린 작업 안내 확인
    client.post(f"/s3/undo/{batch_id}", data={"confirm": "yes"})
    undone_page = client.get(f"/s3?batch={batch_id}&done=1").text
    assert "이미 원래대로 되돌려졌습니다" in undone_page
    undone_md = client.get(f"/s4/catalog/preview?fmt=md&batch={batch_id}").text
    assert "이미 되돌려진 상태" in undone_md


def test_review_findings_all_five_regressions(tmp_path, monkeypatch):
    """리뷰에서 제보된 5건의 회귀 시나리오를 한 번에 검증한다."""
    import re
    from tongdory2.handover import final_catalog as fc_mod

    home = tmp_path.resolve() / "home"
    source = home / "Desktop"
    destination = home / "Documents" / "대규모과제"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)

    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY="")
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    # [이슈 1] 잘못된 JSON 선택 시 편집 중이던 current_paths와 임시저장 유지 플래그 보존
    bad_preview = client.post(
        "/setup/structure/preview",
        data={"current_paths": "내가편집중/폴더 [필수]\n02.설계/화면"},
        files={"document": ("broken.json", b"{not valid json")},
    )
    assert bad_preview.status_code == 422
    assert "내가편집중/폴더 [필수]" in bad_preview.text
    assert 'data-preserve-draft="1"' in bad_preview.text
    assert "편집하던 목록은 아래 칸에 그대로 남겨 두었습니다" in bad_preview.text
    assert 'id="import-confirm-dlg"' in bad_preview.text

    # [이슈 5] 서버 검증: A/B vs A / B 중복 및 보고//회의록 거절
    assert client.post("/setup/structure", data={"paths": "A/B\nA / B"}).status_code == 422
    assert client.post("/setup/structure/export", data={"paths": "A/B\nA / B", "fmt": "json"}).status_code == 422
    assert client.post("/setup/structure", data={"paths": "보고//회의록"}).status_code == 422
    assert client.post("/setup/structure/export", data={"paths": "보고//회의록", "fmt": "json"}).status_code == 422

    # 1~3단계 설정 완료 (실행 전 상태)
    assert client.post(
        "/setup/structure",
        data={"paths": "01.착수/01.계획서 [필수]\n08.보고/02.회의록"},
        follow_redirects=False,
    ).status_code == 303
    assert client.post("/setup/sources", data={"paths": "~/Desktop"}, follow_redirects=False).status_code == 303
    assert client.post("/setup/destination", data={"path": "~/Documents/대규모과제"}, follow_redirects=False).status_code == 303

    # [이슈 3-1] 실행 전이나 존재하지 않는 batch로 ?done=1 열어도 '정리 완료'가 뜨지 않음
    for url in ("/s3?done=1", "/s3?batch=does-not-exist&done=1"):
        page = client.get(url).text
        assert 'id="done-summary"' not in page
        assert "정리 완료" not in page

    # [이슈 3-2] 2건 이동 후 1건만 되돌리기(PARTIAL_UNDO) → 요약과 폴더 집계 모두 1건으로 일치
    old_t = time.time() - 86400
    for name in ("회의록_1.txt", "회의록_2.txt"):
        f = source / name
        f.write_text("회의록 본문 " + name, encoding="utf-8")
        os.utime(f, (old_t, old_t))
    client.get("/s2?new=1")
    deadline = time.monotonic() + 5
    token = None
    while time.monotonic() < deadline:
        m = re.search(r'name="prep_token" value="([^"]+)"', client.get("/s2").text)
        if m:
            token = m.group(1)
            break
        time.sleep(0.02)
    assert token
    client.post("/s2/analyze", data={"prep_token": token})
    deadline = time.monotonic() + 5
    run_id = None
    eids = []
    while time.monotonic() < deadline:
        page = client.get("/s2").text
        rm = re.search(r'name="run_id" value="([^"]+)"', page)
        found = re.findall(r'class="pick" data-id="([^"]+)"', page)
        if rm and len(found) == 2:
            run_id, eids = rm.group(1), found
            break
        time.sleep(0.02)
    assert run_id and len(eids) == 2
    plan_res = client.post("/s3/plan", data={"run_id": run_id, "sel": eids}, follow_redirects=False)
    plan_id = plan_res.headers["location"].rsplit("/", 1)[-1]
    assert client.post(
        f"/s3/execute/{plan_id}",
        data={"confirm": "yes", "extra_confirm": "yes"},
        follow_redirects=False,
    ).status_code == 303
    batch_id = re.search(r"/s3/undo/([^\"]+)", client.get("/s3").text).group(1)

    # 옮긴 파일 2개 중 1개를 수정해 부분 되돌리기(PARTIAL_UNDO) 유도
    moved_1 = destination / "08.보고" / "02.회의록" / "회의록_1.txt"
    moved_1.write_text("이동 뒤 수정된 파일", encoding="utf-8")
    client.post(f"/s3/undo/{batch_id}", data={"confirm": "yes"})

    partial_page = client.get(f"/s3?batch={batch_id}&done=1").text
    assert "일부 되돌림" in partial_page
    assert "정리 완료" not in partial_page
    # 요약 KPI의 이동 건수가 2개가 아니라 1개로 표시되어야 함
    assert "되돌리지 않은 파일</span>\n      <strong>1개</strong>" in partial_page

    # [이슈 4] /s3(이력) 및 /s4(인수인계) 조회 시 read_head 호출 0회, /s3?done=1은 폴더당 표시 한도(4건) 이내
    many_dir = destination / "01.착수" / "01.계획서"
    many_dir.mkdir(parents=True, exist_ok=True)
    for i in range(40):
        (many_dir / f"문서_{i:03d}.txt").write_text("일반 문서", encoding="utf-8")

    orig_read_head = fc_mod.read_head
    head_calls = 0

    def spy_read_head(*args, **kwargs):
        nonlocal head_calls
        head_calls += 1
        return orig_read_head(*args, **kwargs)

    monkeypatch.setattr(fc_mod, "read_head", spy_read_head)
    assert client.get("/s3").status_code == 200
    assert head_calls == 0, f"/s3 must not read file heads, got {head_calls}"
    assert client.get("/s4").status_code == 200
    assert head_calls == 0, f"/s4 must not read file heads, got {head_calls}"
    assert client.get(f"/s3?batch={batch_id}&done=1").status_code == 200
    assert head_calls <= 6, f"/s3?done=1 should only read displayed heads (<=6), got {head_calls}"

    # [이슈 2] 실제 폴더가 1,001개를 넘는 저장 위치에서 내보낸 JSON·TXT도 1단계에서 정상 재불러오기 가능
    monkeypatch.setattr(fc_mod, "read_head", orig_read_head)
    for i in range(1, 1006):
        (destination / f"여분폴더_{i:04d}").mkdir()

    exp_json = client.post("/s4/catalog/export", data={"fmt": "json", "filename": "대규모_기준"})
    assert exp_json.status_code == 200
    payload = exp_json.json()
    assert payload["truncated_folders"] > 0
    reimport_json = client.post(
        "/setup/structure/preview",
        files={"document": ("대규모_기준.json", exp_json.content)},
    )
    assert reimport_json.status_code == 200
    assert "01.착수/01.계획서 [필수]" in reimport_json.text

    exp_txt = client.post("/s4/catalog/export", data={"fmt": "txt", "filename": "대규모_기준"})
    assert exp_txt.status_code == 200
    reimport_txt = client.post(
        "/setup/structure/preview",
        files={"document": ("대규모_기준.txt", exp_txt.content)},
    )
    assert reimport_txt.status_code == 200
    assert "01.착수/01.계획서 [필수]" in reimport_txt.text


def test_message_catalog_and_v3_copy_alignment(tmp_path):
    from tongdory2.messages import MESSAGES, js_messages, msg

    assert MESSAGES and all(isinstance(k, str) and v.strip() for k, v in MESSAGES.items())
    assert msg("setup.import.file.btn") == "파일 선택"
    assert "setup.err.duplicate_line" in js_messages("setup.")

    home = tmp_path.resolve() / "home"
    (home / "Desktop").mkdir(parents=True)
    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY="")
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
    page = client.get("/setup/structure").text
    for expected in (
        'id="tongdory-messages" type="application/json"',
        'id="main-heading" class="page-title" tabindex="-1">1. 분류 희망 폴더 목록</h1>',
        "분류 희망 폴더 목록",
        "많은 파일을 원하는 폴더로 분류할 수 있도록 폴더 구조를 정합니다.",
        "실제로 파일을 모을 위치는 ‘저장 폴더’ 단계에서 선택합니다.",
        "개발 과제에 쓸 수 있는 예시 폴더 13개를 불러옵니다.",
        "선택한 폴더 안의 하위 폴더 목록을 불러옵니다.",
        "JSON·TXT 파일에서 폴더 목록을 불러옵니다.",
        "추천 폴더 목록 사용",
        "추천 목록 불러오기",
        'class="setup-flow-link"',
        "가져온 결과는 아래 폴더 목록과 오른쪽 미리보기에 바로 표시됩니다",
        "분류 희망 폴더 목록 편집",
        ">파일 선택<",
        ">폴더 선택<",
        "한 줄에 폴더 경로 하나씩 적으세요. 필수 산출물을 보관할 폴더 경로 끝에",
        "분류 희망 폴더 목록 확정하고 다음",
        'id="setup-save-guide"',
    ):
        assert expected in page, expected
    for removed in (
        "방금 반영됨",
        "경로 직접 입력",
        "파일 선택 (.json, .txt, .md)",
        "개발 과제 기본 목록 채우기",
        '<a href="/" class="link">상황판</a>',
    ):
        assert removed not in page, removed

    # 2단계(/setup/sources)·3단계(/setup/destination)에서도 번호 부여 제목 및 우측 분류 희망 폴더 목록(.with-tree / #tree-panel) 유지
    assert client.post(
        "/setup/structure",
        data={"paths": "01.착수/01.과제수행계획서 [필수]"},
        follow_redirects=False,
    ).status_code == 303
    sources_page = client.get("/setup/sources").text
    for expected in (
        'id="main-heading" class="page-title" tabindex="-1">2. 정리 대상 폴더</h1>',
        'id="setup-sources-title">정리 대상 폴더 선택</h2>',
        'class="with-tree"',
        'id="tree-panel"',
        "분류 희망 폴더 목록",
    ):
        assert expected in sources_page, expected
    assert "setup-section-number" not in sources_page

    assert client.post("/setup/sources", data={"paths": "~/Desktop"}, follow_redirects=False).status_code == 303
    dest_page = client.get("/setup/destination").text
    for expected in (
        'id="main-heading" class="page-title" tabindex="-1">3. 저장 폴더</h1>',
        'id="setup-dest-title">저장 폴더 선택</h2>',
        'class="with-tree"',
        'id="tree-panel"',
        "분류 희망 폴더 목록",
    ):
        assert expected in dest_page, expected
    assert "setup-section-number" not in dest_page

    for setup_page in (page, sources_page, dest_page):
        match = re.search(r'<script id="tongdory-messages" type="application/json">(.*?)</script>', setup_page, re.S)
        assert match
        rendered_messages = json.loads(match.group(1))
        assert rendered_messages["setup.draft.notice"] == "이전에 작성하던 내용이 있습니다. "
        assert rendered_messages["setup.draft.restore"] == "작성 내용 복원"
        assert rendered_messages["setup.draft.discard"] == "작성 내용 삭제"


def test_setup_pick_folder_and_browse_button_copy(tmp_path, monkeypatch):
    from tongdory2.web.routes import setup as setup_route

    home = tmp_path.resolve() / "home"
    docs_sub = home / "Documents" / "개발자료"
    desktop_sub = home / "Desktop" / "참고"
    docs_sub.mkdir(parents=True)
    desktop_sub.mkdir(parents=True)
    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY="")
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

    # 1) 테스트 환경(PYTEST_CURRENT_TEST 활성)에서는 네이티브 Finder 대신 웹 팝업 폴백 신호 반환
    fallback_resp = client.post("/setup/pick-folder", data={"initial": "~", "multiple": "false"})
    assert fallback_resp.status_code == 200
    assert fallback_resp.json() == {"fallback": True, "paths": []}

    # 2) _pick_folders_macos 결과(단일·다중·취소) 처리 및 tilde 정규화 검증
    recorded_calls = []

    def fake_single(prompt, initial, multiple):
        recorded_calls.append((prompt, initial, multiple))
        return {"paths": [str(docs_sub)]}

    monkeypatch.setattr(setup_route, "_pick_folders_macos", fake_single)
    single_resp = client.post("/setup/pick-folder", data={"initial": "~/Documents", "multiple": "false"})
    assert single_resp.status_code == 200
    assert single_resp.json() == {"paths": ["~/Documents/개발자료"]}
    assert recorded_calls[-1] == ("폴더를 선택하세요", str(home / "Documents"), False)

    def fake_multi(prompt, initial, multiple):
        recorded_calls.append((prompt, initial, multiple))
        return {"paths": [str(desktop_sub), str(docs_sub)]}

    monkeypatch.setattr(setup_route, "_pick_folders_macos", fake_multi)
    multi_resp = client.post("/setup/pick-folder", data={"initial": "~", "multiple": "true", "prompt": "폴더 찾아보기"})
    assert multi_resp.status_code == 200
    assert multi_resp.json() == {"paths": ["~/Desktop/참고", "~/Documents/개발자료"]}
    assert recorded_calls[-1] == ("폴더 찾아보기", str(home), True)

    monkeypatch.setattr(setup_route, "_pick_folders_macos", lambda *_: {"cancelled": True, "paths": []})
    cancel_resp = client.post("/setup/pick-folder", data={"initial": "~", "multiple": "false"})
    assert cancel_resp.status_code == 200
    assert cancel_resp.json() == {"cancelled": True, "paths": []}

    # 3) 2단계(/setup/sources) 버튼 문구 통일 확인
    assert client.post("/setup/structure", data={"paths": "01.착수"}, follow_redirects=False).status_code == 303
    sources_html = client.get("/setup/sources").text
    assert "다른 폴더 찾아보기" not in sources_html
    assert 'data-browse="source-paths" data-browse-multi="1">폴더 찾아보기</button>' in sources_html

    # 4) 프론트엔드 정적 JS에 /setup/pick-folder 연동 코드 포함 확인
    static_dir = Path(setup_route.__file__).resolve().parents[1] / "static"
    for filename in ("setup.js", "catalog_export.js"):
        js_text = (static_dir / filename).read_text(encoding="utf-8")
        assert "/setup/pick-folder" in js_text, filename



