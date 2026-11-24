"""v5.1 UX 9회차: R01/L01 save_sources 이벤트 루프 비차단 및 S4 지식센터 보관 이력 표시 검증."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from tongdory2.config import Settings
from tongdory2.plan.executor import list_batches
from tongdory2.web.app import create_app


def test_save_sources_does_not_block_event_loop_while_setup_lock_held(tmp_path: Path):
    home = tmp_path.resolve() / "home"
    (home / "Desktop").mkdir(parents=True)
    (home / "Documents" / "정리").mkdir(parents=True)

    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY="")
    app = create_app(settings)

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    assert client.post("/setup/structure", data={"paths": "보고/회의록"}, follow_redirects=False).status_code == 303

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as ac:
            held = threading.Event()
            done_hold = threading.Event()

            def hold_lock() -> None:
                with app.state.t.setup_lock:
                    held.set()
                    time.sleep(0.25)
                done_hold.set()

            holder = threading.Thread(target=hold_lock, daemon=True)
            holder.start()
            assert await asyncio.to_thread(held.wait, 2.0)

            post_task = asyncio.create_task(
                ac.post("/setup/sources", data={"paths": "~/Desktop"})
            )
            tick_elapsed: list[float] = []
            while not post_task.done():
                t0 = time.perf_counter()
                await asyncio.sleep(0.02)
                tick_elapsed.append(time.perf_counter() - t0)

            resp = await post_task
            assert await asyncio.to_thread(done_hold.wait, 2.0)

            assert resp.status_code == 303
            assert resp.headers["location"] == "/setup/destination"
            assert len(tick_elapsed) >= 5
            max_delay = max(max(dt - 0.02, 0.0) for dt in tick_elapsed)
            assert max_delay < 0.1
            assert max(tick_elapsed) < 0.1
            assert any(r.path == (home / "Desktop").resolve() for r in app.state.t.db.list_roots())

    asyncio.run(scenario())


def test_s4_kc_history_renders_after_archive(tmp_path: Path):
    home = tmp_path.resolve() / "home"
    desk = home / "Desktop"
    dest = home / "Documents" / "정리"
    desk.mkdir(parents=True)
    dest.mkdir(parents=True)
    (dest / "결과보고서.txt").write_text("최종 보고서", encoding="utf-8")

    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "data"), LINER_API_KEY="")
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    assert client.post("/setup/structure", data={"paths": "보고/결과"}).status_code in (200, 303)
    assert client.post("/setup/sources", data={"paths": "~/Desktop"}).status_code in (200, 303)
    assert client.post("/setup/destination", data={"path": "~/Documents/정리"}).status_code in (200, 303)

    before = client.get("/s4")
    assert before.status_code == 200
    assert 'id="kc-history"' not in before.text

    archive_resp = client.post("/s4/kc", follow_redirects=False)
    assert archive_resp.status_code == 303
    assert archive_resp.headers["location"] == "/s4?archive=1#kc"
    import time as _t
    for _ in range(200):  # 복사는 뒤에서 돈다
        if not (getattr(client.app.state.t, "kc_job", None) or {}).get("running"):
            break
        _t.sleep(0.02)

    kc_batches = [b for b in list_batches(app.state.t.db.conn) if b["summary"].get("label") == "지식센터 보관"]
    assert len(kc_batches) == 1
    batch_id = kc_batches[0]["batch_id"]
    assert kc_batches[0]["status"] == "DONE"

    after = client.get("/s4")
    assert after.status_code == 200
    assert 'id="kc-history"' in after.text
    assert "최근 지식센터 보관 기록" in after.text
    assert f'href="/s3?batch={batch_id}"' in after.text
    assert "보관됨" in after.text
