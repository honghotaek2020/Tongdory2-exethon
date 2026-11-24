"""저장소에 포함된 .env에 API 키가 들어가지 않았는지 확인한다 (커밋 전 실수 방지)."""

import re

from tongdory2.config import PROJECT_ROOT


def test_repo_env_has_no_api_key():
    env = PROJECT_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*(?:export\s+)?LINER_API_KEY\s*=\s*(.*)$", line)
        assert not (m and m.group(1).strip().strip("'\"")), ".env에 LINER_API_KEY 값이 있습니다. 키는 앱 화면에서 입력하세요."
