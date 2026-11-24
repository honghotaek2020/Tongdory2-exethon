"""v5.1 UX 7회차: R02 이동 없이 검토 완료 흐름 및 S3 예상 폴더 트리 모달 검증."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import create_app
from tongdory2.web.setup_state import scope_key


def _make_analyzed_client(tmp_path: Path) -> tuple[TestClient, str]:
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
    client.post("/setup/structure", data={"paths": "보고/회의록"})
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    client.post("/setup/destination", data={"path": "~/Documents/정리"})

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
    return client, run_id


def test_r02_empty_plan_review_only_flow(tmp_path):
    client, run_id = _make_analyzed_client(tmp_path)

    plan_resp = client.post("/s3/plan", data={"run_id": run_id}, follow_redirects=False)
    assert plan_resp.status_code == 303
    plan_url = plan_resp.headers["location"]
    assert plan_url.startswith("/s3/plan/")
    plan_id = plan_url.rsplit("/", 1)[-1]

    plan_page = client.get(plan_url)
    assert plan_page.status_code == 200
    assert "파일 이동 없이 검토 완료" in plan_page.text
    assert 'name="allow_empty" value="1"' in plan_page.text

    blocked = client.post(f"/s3/execute/{plan_id}", data={"confirm": "yes", "extra_confirm": "yes"}, follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"] == "/s2"

    done = client.post(
        f"/s3/execute/{plan_id}",
        data={"allow_empty": "1", "confirm": "yes", "extra_confirm": "yes"},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert done.headers["location"] == "/"

    home_page = client.get("/")
    assert home_page.status_code == 200
    assert "검토 완료 · 파일 이동 없음" in home_page.text
    assert "이번 검토 기록 보기" in home_page.text
    assert "일부 되돌림" not in home_page.text
    assert "이번 작업 되돌리기" not in home_page.text

    s3_page = client.get("/s3")
    assert s3_page.status_code == 200
    assert "검토 완료(이동 없음)" in s3_page.text


def test_s3_renders_expected_folder_tree_modal(tmp_path):
    client, _run_id = _make_analyzed_client(tmp_path)
    resp = client.get("/s3")
    assert resp.status_code == 200
    assert "open-tree-zoom" in resp.text and "tree-panel" in resp.text
    assert "tree-zoom-dlg" in resp.text
