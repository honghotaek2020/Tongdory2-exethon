"""v5.1 UX 12회차(추가 2/5): 사이드바 CSS 특이도·반응형·다크모드·포커스 링 및 주요 화면 접근성 속성 검증."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.liner.client import API_CALLS_DDL
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import create_app
from tongdory2.web.setup_state import scope_key


def _make_full_client(tmp_path: Path, paths: str = "보고/회의록") -> tuple[TestClient, str]:
    home = tmp_path.resolve() / "home"
    desk = home / "Desktop"
    dest = home / "Documents" / "정리"
    desk.mkdir(parents=True)
    dest.mkdir(parents=True)
    sample = desk / "회의록.txt"
    sample.write_text("가상 회의록", encoding="utf-8")

    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY="")
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    assert client.post("/setup/structure", data={"paths": paths}).status_code in (200, 303)
    assert client.post("/setup/sources", data={"paths": "~/Desktop"}).status_code in (200, 303)
    assert client.post("/setup/destination", data={"path": "~/Documents/정리"}).status_code in (200, 303)

    t = app.state.t
    spec = SpecStore(t.db.conn).current()
    catalog = CatalogStore(t.db.conn)
    run_id = catalog.start(spec["version"], {"scope_key": scope_key(t.db.list_roots(), spec)})
    catalog.finish(
        run_id,
        [
            {
                "entry_id": "e0000",
                "path": str(sample),
                "name": sample.name,
                "verdict": "SPEC_NODE",
                "node_path": "보고/회의록",
                "sensitivity": "L1",
                "default_checked": False,
            }
        ],
        0,
    )

    t.db.conn.executescript(API_CALLS_DDL)
    with t.db.conn:
        t.db.conn.execute(
            """
            INSERT INTO api_calls(call_id, run_id, endpoint, model, request_id, cost_usd, cost_kind, latency_ms, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("call-r12", run_id, "/decisions", "gpt-test", "req-r12", 0.001, "REPORTED", 10, "2026-11-20T05:10:00Z"),
        )

    return client, run_id


def test_static_app_css_sidebar_specificity_responsive_dark_and_focus(tmp_path: Path):
    client, _run_id = _make_full_client(tmp_path)

    resp = client.get("/static/app.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers.get("content-type", "")
    css = resp.text

    # 기존 4열 그리드 규칙 뒤에 .side nav.stepbar가 위치해 세로 1열 flex로 덮어씀
    grid_idx = css.index("nav.stepbar { display: grid; grid-template-columns: repeat(4, 1fr);")
    side_idx = css.index(".nav-group, .side nav.stepbar { display: flex; flex-direction: column;")
    assert side_idx > grid_idx
    assert ".side a.navbtn, .side nav.stepbar a.step" in css
    assert ".side a.navbtn.done, .side nav.stepbar a.step.done" in css
    assert ".side a.navbtn.active, .side nav.stepbar a.step.active" in css
    # 진행 단계는 문구로 표시하므로 사이드바에서는 별도 테두리나 배경을 지정하지 않는다.
    assert all(".now" not in line for line in css.splitlines() if ".side" in line)

    # 960px 이하 가로 스크롤 규칙
    m_960 = re.search(r"@media\s*\(max-width:\s*960px\)\s*\{(.*?)\n\}", css, re.DOTALL)
    assert m_960 is not None
    block_960 = m_960.group(1)
    assert ".nav-group, .side nav.stepbar { flex-direction: row; overflow-x: auto; }" in block_960
    assert ".side a.navbtn, .side nav.stepbar a.step { white-space: nowrap; flex-shrink: 0; }" in block_960

    # :focus-visible 포커스 링 규칙
    assert ".side a.navbtn:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }" in css

    # 다크모드 .soft-past-bar 규칙
    dark_blocks = re.findall(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{(.*?)\n\}", css, re.DOTALL)
    assert any(".soft-past-bar {" in b for b in dark_blocks)

    # .groups 그리드 가로폭 및 접을 수 있는 트리 행 커서 규칙
    assert ".groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))" in css
    assert ".tree .trow.has-children { cursor: pointer; }" in css


def test_main_pages_aria_and_dialog_attributes(tmp_path: Path):
    client, run_id = _make_full_client(tmp_path)

    # S3 실행 이력 생성(작업 유형 필터 및 tree 모달 확인용)
    plan_resp = client.post("/s3/plan", data={"run_id": run_id}, follow_redirects=False)
    assert plan_resp.status_code == 303
    plan_id = plan_resp.headers["location"].rsplit("/", 1)[-1]
    exec_resp = client.post(
        f"/s3/execute/{plan_id}",
        data={"allow_empty": "1", "confirm": "yes", "extra_confirm": "yes"},
        follow_redirects=False,
    )
    assert exec_resp.status_code == 303

    # 1) / (상황판)
    home = client.get("/")
    assert home.status_code == 200
    assert 'aria-label="주 메뉴"' in home.text
    assert 'aria-label="진행 단계"' in home.text
    assert 'aria-label="추가 기능"' in home.text
    assert 'aria-labelledby="liner-usage-title"' in home.text
    assert 'aria-haspopup="dialog"' in home.text
    assert 'aria-controls="liner-history"' in home.text
    assert '<dialog id="liner-history" class="liner-history" aria-labelledby="liner-history-title">' in home.text
    assert '<dialog id="tree-zoom-dlg" class="tree-zoom" aria-labelledby="tz-title">' in home.text
    assert 'aria-label="API 호출 검색"' in home.text

    # 2) /s3 (실행 이력)
    s3 = client.get("/s3")
    assert s3.status_code == 200
    assert 'aria-label="주 메뉴"' in s3.text
    assert 'aria-label="진행 단계"' in s3.text
    assert 'aria-label="추가 기능"' in s3.text
    assert 'role="group" aria-label="작업 유형 필터"' in s3.text
    assert '<dialog id="tree-zoom-dlg" class="tree-zoom" aria-labelledby="tz-title">' in s3.text

    # 3) /s4 (인수인계·지식센터)
    s4 = client.get("/s4")
    assert s4.status_code == 200
    assert 'aria-label="주 메뉴"' in s4.text
    assert 'aria-label="진행 단계"' in s4.text
    assert 'aria-label="추가 기능"' in s4.text
    assert "인수인계 초안" in s4.text
    assert "지식센터 보관" in s4.text
    assert "복사할 파일 확인" in s4.text


def test_tree_expand_collapse_buttons_in_panel_and_zoom_dialog(tmp_path: Path):
    # 1) tree|length <= 6일 때는 setup 화면에서만 '모두 펼치기'/'모두 접기'를 렌더링한다
    small_client, small_run_id = _make_full_client(tmp_path / "small")
    small_plan_id = small_client.post("/s3/plan", data={"run_id": small_run_id}, follow_redirects=False).headers["location"].rsplit("/", 1)[-1]
    small_client.post(f"/s3/execute/{small_plan_id}", data={"allow_empty": "1", "confirm": "yes", "extra_confirm": "yes"})
    for path, expect_buttons in (("/", False), ("/setup/structure", True), ("/s3", False)):
        html = small_client.get(path).text
        m_panel = re.search(r'<aside class="tree-aside".*?</aside>', html, re.DOTALL)
        assert m_panel is not None, f"missing tree-aside on {path}"
        panel_html = m_panel.group(0)
        assert ('data-tree-expand' in panel_html) is expect_buttons, path
        assert ('data-tree-collapse' in panel_html) is expect_buttons, path

    # 2) tree|length > 6일 때는 /, /setup/structure, /s3 모두 패널과 확대 모달에 버튼이 렌더링된다
    client, run_id = _make_full_client(
        tmp_path / "large",
        paths="01.착수/01.계획\n02.설계/01.화면\n03.구현/01.코드\n04.시험/01.결과",
    )
    plan_resp = client.post("/s3/plan", data={"run_id": run_id}, follow_redirects=False)
    assert plan_resp.status_code == 303
    plan_id = plan_resp.headers["location"].rsplit("/", 1)[-1]
    exec_resp = client.post(
        f"/s3/execute/{plan_id}",
        data={"allow_empty": "1", "confirm": "yes", "extra_confirm": "yes"},
        follow_redirects=False,
    )
    assert exec_resp.status_code == 303

    for path in ("/", "/setup/structure", "/s3"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        html = resp.text

        m_panel = re.search(r'<aside class="tree-aside".*?</aside>', html, re.DOTALL)
        assert m_panel is not None, f"missing tree-aside on {path}"
        panel_html = m_panel.group(0)
        assert 'data-tree-expand' in panel_html and '모두 펼치기' in panel_html, path
        assert 'data-tree-collapse' in panel_html and '모두 접기' in panel_html, path

        m_dlg = re.search(r'<dialog id="tree-zoom-dlg".*?</dialog>', html, re.DOTALL)
        assert m_dlg is not None, f"missing tree-zoom-dlg on {path}"
        dlg_html = m_dlg.group(0)
        assert 'data-tree-expand' in dlg_html and '모두 펼치기' in dlg_html, path
        assert 'data-tree-collapse' in dlg_html and '모두 접기' in dlg_html, path
