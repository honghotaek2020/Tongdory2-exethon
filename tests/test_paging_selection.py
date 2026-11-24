"""결과를 쪽으로 나눠 보여 주고, 체크는 서버에 저장해 쪽을 옮겨도 유지 · 계획은 저장된 선택으로."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import create_app
from tongdory2.web.routes import s2
from tongdory2.web.setup_state import scope_key


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(s2, "PAGE_SIZE", 3)
    home = tmp_path.resolve() / "home"
    desk = home / "Desktop"
    dest = home / "Documents" / "정리"
    desk.mkdir(parents=True)
    dest.mkdir(parents=True)
    app = create_app(Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(home.parent / "app"), LINER_API_KEY=""))
    c = TestClient(app, base_url="http://127.0.0.1:8765", follow_redirects=False)
    c.post("/setup/structure", data={"paths": "보고/회의록"})
    c.post("/setup/sources", data={"paths": "~/Desktop"})
    c.post("/setup/destination", data={"path": "~/Documents/정리"})
    t = app.state.t
    spec = SpecStore(t.db.conn).current()
    catalog = CatalogStore(t.db.conn)
    rid = catalog.start(spec["version"], {"scope_key": scope_key(t.db.list_roots(), spec)})
    entries = []
    for i in range(7):
        f = desk / f"회의록_{i}.txt"
        f.write_text("가상")
        entries.append({"entry_id": f"e{i:04d}", "path": str(f), "name": f.name, "verdict": "SPEC_NODE",
                        "node_path": "보고/회의록", "sensitivity": "L1", "default_checked": i != 6})
    catalog.finish(rid, entries, 0)
    return c, catalog, rid


def ids_on(page: str) -> list[str]:
    return re.findall(r'class="pick" data-id="([^"]+)"', page)


def test_paging_shows_page_size_and_selection_survives_pages(env):
    c, catalog, rid = env
    p1 = c.get("/s2?tab=plan").text
    assert ids_on(p1) == ["e0000", "e0001", "e0002"] and "1 / 3쪽" in p1 and "필요한 폴더를 만든 뒤 정리" in p1
    assert ids_on(c.get("/s2?tab=plan&page=3").text) == ["e0006"]
    # 1쪽에서 체크 해제 → 서버 저장 → 다른 쪽에 갔다 와도 유지
    r = c.post("/s2/check", data={"run_id": rid, "entry_id": "e0001", "checked": "0"})
    assert r.json()["checked"] == 6  # 화면을 열면 추천 폴더가 있는 e0006도 기본 선택(예전 결과 보정)
    c.get("/s2?tab=plan&page=2")
    p1 = c.get("/s2?tab=plan&page=1").text
    tag = re.search(r'<input type="checkbox" class="pick" data-id="e0001"[^>]*>', p1).group(0)
    assert "checked" not in tag
    # 계획은 화면 체크를 보내지 않고 서버에 저장된 선택으로
    r = c.post("/s3/plan", data={"run_id": rid, "use_saved": "1"})
    plan = c.get(r.headers["location"]).text
    assert "회의록_1.txt" not in plan and "회의록_6.txt" in plan and "회의록_5.txt" in plan


def test_bulk_safe_all_applies_to_whole_batch(env):
    c, catalog, rid = env
    c.post("/s2/check", data={"run_id": rid, "entry_id": ["e0000", "e0004"], "checked": "0"})
    assert len(catalog.effective_selection(rid)) == 4
    r = c.post("/s2/check", data={"run_id": rid, "action": "safe_all"}, headers={"referer": "http://127.0.0.1:8765/s2?tab=plan&page=2"})
    assert r.status_code == 303, r.text
    assert catalog.effective_selection(rid) == {f"e{i:04d}" for i in range(6)}  # e0006(기본 해제)은 그대로


def test_check_rejects_old_run(env):
    c, _, _ = env
    assert c.post("/s2/check", data={"run_id": "old", "entry_id": "e0000"}).status_code == 409
