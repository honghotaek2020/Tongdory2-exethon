"""공통 테스트 도우미.

파일 종류 확인 화면(S2 검사 전 제외 선택)이 끼어들면 모든 종류를 포함한 채로 넘긴다 —
기존 흐름 테스트가 제외 기능과 무관하게 이전과 같은 결과를 확인하도록.
"""

from __future__ import annotations

import re

from starlette.testclient import TestClient

_orig_get = TestClient.get


def _get_passing_filter(self, url, *args, **kwargs):
    r = _orig_get(self, url, *args, **kwargs)
    if 'action="/s2/filter"' in r.text and str(url).startswith("/s2"):
        token = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
        exts = re.findall(r'name="keep_ext" value="([^"]+)"', r.text)
        self.post("/s2/filter", data={"token": token, "keep_ext": exts}, follow_redirects=False)
        r = _orig_get(self, url, *args, **kwargs)
    return r


_get_passing_filter.raw = _orig_get
TestClient.get = _get_passing_filter


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _keep_batch_split(request, monkeypatch):
    """기존 묶음 테스트는 작은 상한(MAX_ITEMS)으로 묶음 나누기를 확인한다 — 한 번에 검사 기능은 끈다.
    한 번에 검사를 확인하는 테스트는 `@pytest.mark.one_batch`로 켠다."""
    if request.node.get_closest_marker("one_batch") is None:
        from tongdory2.web.routes import s2

        monkeypatch.setattr(s2, "ONE_BATCH_MAX", 0)
    if request.node.get_closest_marker("auto_series") is None:
        from tongdory2.web.routes import s2 as _s2

        monkeypatch.setattr(_s2, "AUTO_CONTINUE", False)  # 기존 테스트는 묶음마다 직접 넘기는 흐름


def pytest_configure(config):
    config.addinivalue_line("markers", "one_batch: 걸러낸 뒤 적으면 한 번에 검사하는 기능을 켠다")
    config.addinivalue_line("markers", "auto_series: 남은 묶음 자동 이어 검사를 켠다")
