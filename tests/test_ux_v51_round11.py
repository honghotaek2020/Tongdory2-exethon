"""v5.1 UX 11회차(추가 1/5): 엣지케이스 심화 검증 및 트리 빈 필터 안내 보완."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.liner.client import API_CALLS_DDL
from tongdory2.plan.executor import list_batches
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import create_app
from tongdory2.web.setup_state import scope_key


def _make_analyzed_client(tmp_path: Path) -> tuple[TestClient, str, Path, Path]:
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
    assert client.post("/setup/structure", data={"paths": "보고/회의록"}).status_code in (200, 303)
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
    return client, run_id, desk, dest


def test_r02_double_submit_blocked_and_no_duplicate_batch(tmp_path: Path):
    client, run_id, _desk, _dest = _make_analyzed_client(tmp_path)

    plan_resp = client.post("/s3/plan", data={"run_id": run_id}, follow_redirects=False)
    assert plan_resp.status_code == 303
    plan_id = plan_resp.headers["location"].rsplit("/", 1)[-1]

    first = client.post(
        f"/s3/execute/{plan_id}",
        data={"allow_empty": "1", "confirm": "yes", "extra_confirm": "yes"},
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert first.headers["location"] == "/"
    assert len(list_batches(client.app.state.t.db.conn)) == 1

    second = client.post(
        f"/s3/execute/{plan_id}",
        data={"allow_empty": "1", "confirm": "yes", "extra_confirm": "yes"},
        follow_redirects=False,
    )
    assert second.status_code == 303
    assert second.headers["location"] == "/s3"
    assert len(list_batches(client.app.state.t.db.conn)) == 1


def test_r02_stale_empty_plan_blocked_after_check_or_override_change(tmp_path: Path):
    client, run_id, _desk, _dest = _make_analyzed_client(tmp_path)

    # 1) 체크 상태 변경 후 기존 0개 이동 계획 실행 차단
    plan_check = client.post("/s3/plan", data={"run_id": run_id}, follow_redirects=False)
    assert plan_check.status_code == 303
    plan_check_id = plan_check.headers["location"].rsplit("/", 1)[-1]

    chk = client.post(
        "/s2/check",
        data={"run_id": run_id, "entry_id": "e0000", "checked": "1"},
    )
    assert chk.status_code == 200

    stale_check = client.post(
        f"/s3/execute/{plan_check_id}",
        data={"allow_empty": "1", "confirm": "yes", "extra_confirm": "yes"},
        follow_redirects=False,
    )
    assert stale_check.status_code == 303
    assert stale_check.headers["location"] == "/s2"
    assert len(list_batches(client.app.state.t.db.conn)) == 0

    # 2) 항목 지정(override) 변경 후 기존 0개 이동 계획 실행 차단
    plan_override = client.post("/s3/plan", data={"run_id": run_id}, follow_redirects=False)
    assert plan_override.status_code == 303
    plan_override_id = plan_override.headers["location"].rsplit("/", 1)[-1]

    spec = SpecStore(client.app.state.t.db.conn).current()
    dest_node_id = next(
        n["node_id"]
        for n in spec["body"]["nodes"]
        if n["status"] == "ADOPTED" and n["role"] == "DEST"
    )
    assign = client.post(
        "/s2/assign",
        data={"run_id": run_id, "entry_ids": "e0000", "target": dest_node_id},
        follow_redirects=False,
    )
    assert assign.status_code == 303

    stale_override = client.post(
        f"/s3/execute/{plan_override_id}",
        data={"allow_empty": "1", "confirm": "yes", "extra_confirm": "yes"},
        follow_redirects=False,
    )
    assert stale_override.status_code == 303
    assert stale_override.headers["location"] == "/s2"
    assert len(list_batches(client.app.state.t.db.conn)) == 0


def test_s4_draft_viewer_path_safety(tmp_path: Path):
    client, _run_id, _desk, dest = _make_analyzed_client(tmp_path)

    (dest / "README.md").write_text("# 일반 README", encoding="utf-8")
    (dest / "README_인수인계_0.md").write_text("# 0번 초안", encoding="utf-8")
    (dest.parent / "secret.md").write_text("상위 폴더 비밀 문서", encoding="utf-8")
    (dest / "README_인수인계.md").write_text("# 정상 인수인계 초안", encoding="utf-8")

    assert client.get("/s4/draft/README.md").status_code == 404
    assert client.get("/s4/draft/README_인수인계_0.md").status_code == 404
    assert client.get("/s4/draft/..%2Fsecret.md").status_code == 404
    assert client.get("/s4/draft/README_인수인계.md").status_code == 200


def test_liner_usage_csv_outputs_none_cost_kind(tmp_path: Path):
    client, run_id, _desk, _dest = _make_analyzed_client(tmp_path)
    conn = client.app.state.t.db.conn
    conn.executescript(API_CALLS_DDL)
    with conn:
        conn.execute(
            """
            INSERT INTO api_calls(call_id, run_id, endpoint, model, request_id, cost_usd, cost_kind, latency_ms, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("call-none-1", run_id, "/decisions", "gpt-test", "req-1", None, "NONE", 12, "2026-11-20T05:00:00Z"),
        )

    csv_resp = client.get("/liner/usage.csv")
    assert csv_resp.status_code == 200
    body = csv_resp.content.decode("utf-8-sig")
    assert "Decisions · 분류 판정" in body
    assert "외부 호출" in body
    assert "비용 미기록" in body
    assert ",NONE" in body


def test_tree_filter_empty_state_message_in_home_and_s2(tmp_path: Path):
    client, _run_id, _desk, _dest = _make_analyzed_client(tmp_path)

    home_resp = client.get("/")
    assert home_resp.status_code == 200
    assert "조건에 맞는 폴더가 없습니다." in home_resp.text

    s2_resp = client.get("/s2?result=1")
    assert s2_resp.status_code == 200
    assert "조건에 맞는 폴더가 없습니다." in s2_resp.text
