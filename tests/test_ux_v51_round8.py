"""v5.1 UX 8회차: 과거 단계 안내 배너(executed 포함·해제) 및 S3/S4 쉬운 한국어 문구 검증."""

from __future__ import annotations

import re

from pathlib import Path

from fastapi.testclient import TestClient

from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.spec.store import SpecStore
from tongdory2.web import progress
from tongdory2.web.app import create_app
from tongdory2.web.setup_state import scope_key


def _make_setup_client(tmp_path: Path) -> tuple[TestClient, Path]:
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
    return client, sample


def _complete_setup(client: TestClient) -> None:
    assert client.post("/setup/structure", data={"paths": "보고/회의록"}).status_code in (200, 303)
    assert client.post("/setup/sources", data={"paths": "~/Desktop"}).status_code in (200, 303)
    assert client.post("/setup/destination", data={"path": "~/Documents/정리"}).status_code in (200, 303)


def _add_analyzed_run(client: TestClient, sample: Path, *, verdict: str = "SPEC_NODE") -> str:
    t = client.app.state.t
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
                "verdict": verdict,
                "node_path": "보고/회의록",
                "sensitivity": "L1",
                "default_checked": False,
            }
        ],
        0,
    )
    return run_id


def test_soft_past_bar_renders_dismiss_and_return_link_in_past_step(tmp_path):
    client, sample = _make_setup_client(tmp_path)

    # 설정 미완료(cur_step=0)에서는 /setup/structure에 배너·읽기 전용 잠금이 뜨지 않음
    before = client.get("/setup/structure")
    assert before.status_code == 200
    assert 'id="soft-past-bar"' not in before.text
    assert 'id="readonly-bar"' not in before.text

    # 설정 완료만 한 상태(분석 시작 전)에서는 아직 자유롭게 수정 가능하므로 배너·잠금이 뜨지 않음
    _complete_setup(client)
    setup_only = client.get("/setup/structure")
    assert setup_only.status_code == 200
    assert 'id="soft-past-bar"' not in setup_only.text
    assert 'id="readonly-bar"' not in setup_only.text

    # 분석 시작/완료 뒤(/s2 진행 상태) /setup/structure 진입 시 안내 배너와 읽기 전용 잠금 표시
    _add_analyzed_run(client, sample, verdict="HOLD")

    past_setup = client.get("/setup/structure")
    assert past_setup.status_code == 200
    assert 'class="notice soft-past-bar"' in past_setup.text
    assert 'id="soft-past-bar"' in past_setup.text
    assert 'id="readonly-bar"' in past_setup.text
    assert 'id="open-unlock"' in past_setup.text
    assert 'id="dismiss-past-bar"' in past_setup.text
    assert "이 화면 계속 보기" in past_setup.text
    assert "현재 단계로 돌아가기 →" in past_setup.text
    assert 'href="/s2"' in past_setup.text
    assert "<b>분석·검토</b> 단계까지 진행했습니다" in past_setup.text

    # 현재 단계(/s2) 진입 시에는 배너가 뜨지 않음
    current_s2 = client.get("/s2")
    assert current_s2.status_code == 200
    assert 'id="soft-past-bar"' not in current_s2.text


def test_soft_past_bar_renders_when_executed_cur_step_3(tmp_path):
    client, sample = _make_setup_client(tmp_path)
    _complete_setup(client)
    run_id = _add_analyzed_run(client, sample)

    plan_resp = client.post("/s3/plan", data={"run_id": run_id}, follow_redirects=False)
    assert plan_resp.status_code == 303
    plan_id = plan_resp.headers["location"].rsplit("/", 1)[-1]

    done = client.post(
        f"/s3/execute/{plan_id}",
        data={"allow_empty": "1", "confirm": "yes", "extra_confirm": "yes"},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert done.headers["location"] == "/"

    # executed=True, cur_step=3 상태 확인 후 /setup/structure 및 /s2 진입
    st = progress.state(client.app.state.t)
    assert st["executed"] is True
    assert st["cur_step"] == 3

    for path in ("/setup/structure", "/s2"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert 'class="notice soft-past-bar"' in resp.text
        assert 'id="soft-past-bar"' in resp.text
        assert 'id="dismiss-past-bar"' in resp.text
        assert "이 화면 계속 보기" in resp.text
        assert "past-dismiss:" in client.get("/static/web-ui.js").text
        assert "<b>완료</b> 단계까지 진행했습니다" in resp.text
        assert re.search(r'<a class="btn small primary" href="/s3\?batch=[^"]+&amp;done=1">현재 단계로 돌아가기 →</a>', resp.text)

    # /s2a, /s2/standards 등 보조 경로는 과거 단계 배너 대상에서 제외됨
    for aux_path in ("/s2a", "/s2/standards"):
        aux = client.get(aux_path)
        assert aux.status_code == 200
        assert 'id="soft-past-bar"' not in aux.text


def test_s3_and_s4_plain_korean_copy(tmp_path):
    client, _sample = _make_setup_client(tmp_path)
    _complete_setup(client)

    s3_resp = client.get("/s3")
    assert s3_resp.status_code == 200
    assert "작업별 이동 내역을 확인하고 되돌리기를 요청할 수 있습니다" in s3_resp.text

    s4_resp = client.get("/s4")
    assert s4_resp.status_code == 200
    assert "분류 희망 폴더 목록을 현재 저장 폴더와 비교했습니다" in s4_resp.text
    assert "복사할 파일 확인" in s4_resp.text


def test_notice_modal_and_soft_past_backdrop_render(tmp_path):
    client, sample = _make_setup_client(tmp_path)

    # 설정 미완료 상태에서는 #notice-modal은 포함되지만 #soft-past-backdrop은 없음
    before = client.get("/setup/structure")
    assert before.status_code == 200
    assert '<dialog id="notice-modal" class="notice-modal" aria-labelledby="notice-modal-title" closedby="any">' in before.text
    assert 'id="notice-modal-ok"' in before.text
    assert 'id="soft-past-backdrop"' not in before.text

    # 설정만 완료한 상태(분석 시작 전)에서는 아직 잠기지 않으므로 #soft-past-backdrop/#soft-past-bar 없음
    _complete_setup(client)
    setup_only = client.get("/setup/structure")
    assert setup_only.status_code == 200
    assert 'id="soft-past-backdrop"' not in setup_only.text
    assert 'id="soft-past-bar"' not in setup_only.text

    # 분석 후 이전 단계 안내는 네이티브 dialog와 ::backdrop을 사용한다.
    _add_analyzed_run(client, sample)
    past = client.get("/setup/structure")
    assert past.status_code == 200
    assert 'id="soft-past-backdrop"' not in past.text
    assert '<dialog class="notice soft-past-bar"' in past.text
    assert 'id="soft-past-bar"' in past.text

    # 새 정리 시작 flash 안내는 data-notice-modal 마커를 가짐
    reset_resp = client.post("/new-run", data={"confirm": "yes"})
    assert reset_resp.status_code == 200
    assert 'data-notice-modal' in reset_resp.text

    # CSS에 레이아웃 밀림 방지 숨김 규칙 및 모달 스타일 존재
    css = client.get("/static/app.css").text
    assert "[data-notice-modal], #step-content > .notice { display: none; }" in css
    assert "dialog.notice-modal" in css
    assert ".soft-past-bar::backdrop" in css
