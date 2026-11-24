"""v5.1 UX 6회차: Liner 사용 이력 필터·검색·CSV 내려받기 및 S3 미해결 배지 검증."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from tongdory2.config import Settings
from tongdory2.liner import client as liner
from tongdory2.plan.executor import init as init_plan_tables
from tongdory2.web.app import create_app


def _make_client(tmp_path: Path):
    home = tmp_path.resolve() / "home"
    (home / "Desktop").mkdir(parents=True)
    (home / "Documents" / "정리").mkdir(parents=True)
    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY="")
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    return client, app.state.t.db.conn


def test_liner_usage_csv_empty_returns_bom_and_header(tmp_path):
    client, _conn = _make_client(tmp_path)
    resp = client.get("/liner/usage.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"] == 'attachment; filename="liner_usage.csv"'
    assert resp.content.startswith(b"\xef\xbb\xbf")
    assert resp.text.lstrip("\ufeff").splitlines()[0] == "시각,API,처리,비용(추정값 포함)(USD),비용 유형"


def test_liner_usage_csv_and_home_filters_with_calls(tmp_path):
    client, conn = _make_client(tmp_path)
    conn.executescript(liner.API_CALLS_DDL)
    with conn:
        conn.execute(
            "INSERT INTO api_calls VALUES(?,?,?,?,?,?,?,?,?)",
            ("c_cached", "run_1", "/decisions", None, None, 0.0, "CACHED", 0, "2026-11-20T01:00:00+00:00"),
        )
        # API_CALLS_DDL CHECK 제약에 따라 실제 외부 호출 비용 유형은 REPORTED 사용
        conn.execute(
            "INSERT INTO api_calls VALUES(?,?,?,?,?,?,?,?,?)",
            ("c_ext", "run_1", "/embeddings", None, None, 0.0014, "REPORTED", 120, "2026-11-20T01:01:00+00:00"),
        )

    csv_resp = client.get("/liner/usage.csv")
    assert csv_resp.status_code == 200
    body = csv_resp.text
    assert "저장된 응답 재사용" in body
    assert "캐시 사용" in body
    assert "외부 호출" in body
    assert "0.0014" in body

    home_resp = client.get("/")
    assert home_resp.status_code == 200
    html = home_resp.text
    assert "liner-search" in html
    assert 'data-lfilter="cached"' in html
    assert 'href="/liner/usage.csv"' in html


def test_s3_shows_unresolved_badge_when_batch_has_denied(tmp_path):
    client, conn = _make_client(tmp_path)
    init_plan_tables(conn)
    with conn:
        conn.execute(
            "INSERT INTO batches(batch_id, plan_id, status, summary, created_at, undone_at) VALUES(?,?,?,?,?,NULL)",
            (
                "batch_r6",
                None,
                "DONE",
                json.dumps({"moved": 3, "created_dirs": 1, "denied": 2}),
                "2026-11-20T01:05:00+00:00",
            ),
        )

    resp = client.get("/s3")
    assert resp.status_code == 200
    assert "미해결 2" in resp.text
