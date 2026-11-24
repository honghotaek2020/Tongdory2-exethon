"""v5.1 UX 14회차(추가 4/5): 잠금 동시성·비차단 읽기·디스크 스캔 금지·트리 집계 성능 불변식 검증."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.liner.client import API_CALLS_DDL
from tongdory2.spec.store import SpecStore
from tongdory2.web import progress
from tongdory2.web.app import create_app
from tongdory2.web.setup_state import scope_key


def _setup_app(tmp_path: Path, *, with_downloads: bool = False) -> tuple[object, TestClient, Path, Path]:
    home = tmp_path.resolve() / "home"
    desk = home / "Desktop"
    dest = home / "Documents" / "정리"
    desk.mkdir(parents=True)
    dest.mkdir(parents=True)
    if with_downloads:
        (home / "Downloads").mkdir(parents=True)
    (desk / "회의록.txt").write_text("가상 회의록", encoding="utf-8")

    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY="")
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    assert client.post("/setup/structure", data={"paths": "보고/회의록"}).status_code in (200, 303)
    assert client.post("/setup/sources", data={"paths": "~/Desktop"}).status_code in (200, 303)
    assert client.post("/setup/destination", data={"path": "~/Documents/정리"}).status_code in (200, 303)
    return app, client, desk, dest


def test_nonblocking_reads_and_event_loop_while_setup_lock_held(tmp_path: Path):
    app, _client, _desk, _dest = _setup_app(tmp_path, with_downloads=True)

    # /liner/usage.csv가 실제 호출 이력을 직렬화하도록 샘플 이력 1건 기록
    conn = sqlite3.connect(app.state.t.settings.db_path)
    conn.executescript(API_CALLS_DDL)
    conn.execute(
        "INSERT INTO api_calls(endpoint, model, cost_kind, cost_usd, created_at) "
        "VALUES('/chat/completions', 'liner-pro', 'ESTIMATED', 0.0012, '2026-11-20T05:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as ac:
            held = threading.Event()
            done_hold = threading.Event()

            def hold_lock() -> None:
                with app.state.t.setup_lock:
                    held.set()
                    time.sleep(0.30)
                done_hold.set()

            holder = threading.Thread(target=hold_lock, daemon=True)
            holder.start()
            assert await asyncio.to_thread(held.wait, 2.0)

            async def timed_get(path: str):
                t0 = time.perf_counter()
                resp = await ac.get(path)
                return path, resp, time.perf_counter() - t0

            post_filter = asyncio.create_task(
                ac.post("/s2/filter", data={"token": "stale", "keep_ext": "txt"})
            )
            post_sources = asyncio.create_task(
                ac.post("/setup/sources", data={"paths": "~/Downloads"})
            )
            read_group = asyncio.gather(
                timed_get("/"),
                timed_get("/s3"),
                timed_get("/liner/usage.csv"),
            )

            tick_elapsed: list[float] = []
            while not (post_filter.done() and post_sources.done() and read_group.done()):
                t0 = time.perf_counter()
                await asyncio.sleep(0.02)
                tick_elapsed.append(time.perf_counter() - t0)

            reads = await read_group
            r_filter, r_add = await asyncio.gather(post_filter, post_sources)
            assert await asyncio.to_thread(done_hold.wait, 2.0)

            for path, resp, elapsed in reads:
                assert resp.status_code == 200, f"{path} failed: {resp.status_code}"
                assert elapsed < 0.20, f"{path} blocked on setup_lock: {elapsed:.4f}s"

            assert r_filter.status_code == 303
            assert r_add.status_code == 303
            assert len(tick_elapsed) >= 5
            max_delay = max(max(dt - 0.02, 0.0) for dt in tick_elapsed)
            assert max_delay < 0.10, f"event loop tick delayed: {max_delay:.4f}s"
            assert any(r.path == (tmp_path.resolve() / "home" / "Downloads") for r in app.state.t.db.list_roots())

    asyncio.run(scenario())


def test_progress_state_and_home_do_not_scandir_dest_root(tmp_path: Path, monkeypatch):
    app, client, desk, dest = _setup_app(tmp_path)
    (dest / "보고" / "회의록").mkdir(parents=True)
    (dest / "보고" / "회의록" / "기존자료.txt").write_text("기존", encoding="utf-8")

    t = app.state.t
    spec = SpecStore(t.db.conn).current()
    catalog = CatalogStore(t.db.conn)
    run_id = catalog.start(spec["version"], {"scope_key": scope_key(t.db.list_roots(), spec)})
    catalog.finish(
        run_id,
        [
            {
                "entry_id": "e0000",
                "path": str(desk / "회의록.txt"),
                "name": "회의록.txt",
                "verdict": "SPEC_NODE",
                "node_path": "보고/회의록",
                "sensitivity": "L1",
                "default_checked": True,
            }
        ],
        0,
    )

    real_scandir = os.scandir
    dest_resolved = dest.resolve()

    def forbid_dest_scandir(path="."):
        target = Path(os.fspath(path)).resolve()
        if target == dest_resolved or dest_resolved in target.parents:
            raise AssertionError(f"os.scandir called on DEST_ROOT: {target}")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", forbid_dest_scandir)

    st = progress.state(t)
    assert st["analyzed"] is True
    assert st["current"].path == dest_resolved

    resp = client.get("/")
    assert resp.status_code == 200
    assert "상황판" in resp.text
    assert "회의록" in resp.text

    with pytest.raises(AssertionError):
        os.scandir(dest)


def test_large_nested_new_paths_tree_aggregation_performance(tmp_path: Path):
    app, _client, desk, _dest = _setup_app(tmp_path)
    t = app.state.t
    spec = SpecStore(t.db.conn).current()
    catalog = CatalogStore(t.db.conn)
    run_id = catalog.start(spec["version"], {"scope_key": scope_key(t.db.list_roots(), spec)})

    entries = [
        {
            "entry_id": f"e{i:04d}",
            "path": str(desk / f"doc_{i:03d}.txt"),
            "name": f"doc_{i:03d}.txt",
            "verdict": "NEW_NODE",
            "node_path": f"자동분류/그룹_{i // 10:02d}/하위_{i % 10}",
            "sensitivity": "L1",
            "default_checked": False,
        }
        for i in range(200)
    ]
    catalog.finish(run_id, entries, 0)
    catalog.set_checked(run_id, [e["entry_id"] for e in entries[::2]], True)

    t0 = time.perf_counter()
    st = progress.state(t)
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.25, f"progress.state too slow: {elapsed:.4f}s"
    assert len(st["new_paths"]) == 200

    by_path = {node["path"]: node for node in st["tree"]}
    assert "자동분류" in by_path
    assert by_path["자동분류"]["n"] == 100
    assert by_path["자동분류"]["depth"] == 0
    assert by_path["자동분류"]["new"] is True
    assert by_path["자동분류"]["leaf"] is False

    assert by_path["자동분류/그룹_00"]["n"] == 5
    assert by_path["자동분류/그룹_00"]["depth"] == 1
    assert by_path["자동분류/그룹_00"]["leaf"] is False

    assert by_path["자동분류/그룹_00/하위_0"]["n"] == 1
    assert by_path["자동분류/그룹_00/하위_0"]["depth"] == 2
    assert by_path["자동분류/그룹_00/하위_0"]["leaf"] is True
