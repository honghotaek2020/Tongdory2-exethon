"""파일 종류 살펴보기 → 제외 선택 → 걸러낸 항목만 목록(DB) 저장 → 적으면 한 번에 검사."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tongdory2.catalog import pipeline
from tongdory2.catalog.survey import Exclusions, summarize
from tongdory2.config import Settings
from tongdory2.fsx.roots import Role
from tongdory2.fsx.scan import ScanItem
from tongdory2.web.app import create_app



def _item(rel: str, is_dir: bool = False, root: str = "r1") -> ScanItem:
    return ScanItem(Path("/h/Desktop") / rel, root, Role.COLLECT, rel, is_dir, 1, 0.0, False)


def test_summarize_counts_types_and_finds_concentrated_folder():
    items = [_item(f"eis/gen/{i:05d}/key.pem") for i in range(900)]
    items += [_item(f"eis/perf/{i:04d}/key.pem") for i in range(100)]
    items += [_item("회의록.docx"), _item("docs/설계.md"), _item("README"), _item("sdk", is_dir=True)]
    s = summarize(items, 0, {"r1": "Desktop"})
    by = {e["ext"]: e for e in s.exts}
    assert s.total == 1004 and by["pem"]["count"] == 1000 and by["pem"]["sensitive"]
    # 바로 위 폴더(1,000곳에 1개씩)가 아니라 2단계 위로 묶어서 보여 준다
    assert by["pem"]["folders"][0] == ("eis/gen", 900) and by["pem"]["depth"] == 2
    assert "(확장자 없음)" in by and "(폴더)" in by
    assert s.hotspots and s.hotspots[0]["key"] == "r1|eis/gen"


def test_exclusions_keep_by_extension_and_folder():
    ex = Exclusions({"pem"}, {"r1|eis"})
    assert not ex.keep(_item("a/b.pem"))
    assert not ex.keep(_item("eis/x/설계.docx"))
    assert ex.keep(_item("eisx/설계.docx"))  # 경계: eis 폴더만, eisx는 아님
    assert ex.keep(_item("eis.docx"))
    assert Exclusions.from_json(ex.to_json()) == ex


def _setup(tmp_path: Path):
    home = tmp_path.resolve() / "home"
    desktop = home / "Desktop"
    old = time.time() - 86400
    for i in range(6):
        p = desktop / "eis" / "gen" / f"{i:02d}" / "key.pem"
        p.parent.mkdir(parents=True)
        p.write_text("-----BEGIN PRIVATE KEY-----")
        os.utime(p, (old, old))
    for name in ("회의록_0915.txt", "설계서.txt"):
        p = desktop / name
        p.write_text("가상 자료")
        os.utime(p, (old, old))
    (home / "Documents" / "기존구조" / "보고" / "회의록").mkdir(parents=True)
    (home / "Documents" / "저장").mkdir(parents=True)
    settings = Settings(TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(home.parent / "data"), LINER_API_KEY="")
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.post("/setup/structure", data={"paths": "보고/회의록", "reference": "~/Documents/기존구조"})
    client.post("/setup/sources", data={"paths": "~/Desktop"})
    client.post("/setup/destination", data={"path": "~/Documents/저장"})
    return app, client


def _raw_get(client, url):
    # conftest의 자동 통과 도우미를 거치지 않고 실제 화면을 본다
    from starlette.testclient import TestClient as T

    return T.get.raw(client, url)


def _wait(client, phrase):
    for _ in range(200):
        page = _raw_get(client, "/s2")
        if phrase in page.text:
            return page.text
        time.sleep(0.025)
    raise AssertionError(page.text[:1500])


@pytest.mark.one_batch
def test_filter_screen_excludes_before_db_and_checks_in_one_batch(tmp_path, monkeypatch):
    app, client = _setup(tmp_path)
    monkeypatch.setattr(pipeline, "MAX_ITEMS", 1)  # 원래라면 묶음 3개 — 걸러낸 뒤 적으면 한 번에
    _raw_get(client, "/s2?new=1")
    page = _wait(client, 'action="/s2/filter"')
    assert "발견한 파일 8개" in page and "민감·이동 안 함" in page
    assert re.search(r'name="keep_ext" value="pem"[^>]*data-count="6"\s*>', page)  # 민감 확장자는 기본 체크 해제
    assert "eis/gen" in page  # 몰린 폴더 예시
    token = re.search(r'name="token" value="([^"]+)"', page).group(1)
    client.post("/s2/filter", data={"token": token, "keep_ext": ["txt"]})
    _wait(client, 'name="prep_token"')
    with app.state.t.db.conn as conn:
        names = [r[0] for r in conn.execute("SELECT rel FROM inventory_items")]
    assert sorted(names) == ["설계서.txt", "회의록_0915.txt"]  # pem은 DB에 저장하지 않음
    page = _raw_get(client, "/s2").text
    assert re.search(r"살펴본 항목</th><td>2개", page) and "다음 묶음 대상" not in page  # 한 묶음으로
    assert "<strong>6개</strong>는 목록에 포함하지 않았습니다" in page
    # 다시 분석해도 저장한 제외 설정이 미리 반영된다
    client.post("/s2/rescan")
    page = _wait(client, 'action="/s2/filter"')
    assert re.search(r'name="keep_ext" value="pem"[^>]*data-count="6"\s*>', page)
    assert re.search(r'name="keep_ext" value="txt"[^>]*checked', page)


def test_filter_rejects_stale_token(tmp_path):
    app, client = _setup(tmp_path)
    _raw_get(client, "/s2?new=1")
    _wait(client, 'action="/s2/filter"')
    r = client.post("/s2/filter", data={"token": "bad", "keep_ext": ["txt"]}, follow_redirects=False)
    assert r.headers["location"] == "/s2?new=1"
