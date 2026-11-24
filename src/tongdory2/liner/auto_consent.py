"""파일 분석 단계에서 Liner 자동 개입(파일명·폴더명으로 폴더 추천)에 대한 사용자 동의.

최초 1회만 묻는다. 상태: None(아직 묻지 않음) | True(동의) | False(거절).
앱 데이터 폴더의 `liner_auto.json`에 저장한다. 바꾸려면 상황판 'Liner 사용 현황'의 체크박스를 쓴다.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

FILE_NAME = "liner_auto.json"


def consent_file(data_dir: Path) -> Path:
    return data_dir / FILE_NAME


def load(data_dir: Path) -> bool | None:
    try:
        value = json.loads(consent_file(data_dir).read_text(encoding="utf-8")).get("auto")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, bool) else None


def save(data_dir: Path, agreed: bool) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"auto": agreed, "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".liner_auto-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, consent_file(data_dir))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
