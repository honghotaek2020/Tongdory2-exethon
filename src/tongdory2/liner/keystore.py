"""화면에서 입력한 Liner API 키를 저장·삭제하고, 실제로 쓰는 키를 판정한다.

키는 앱 데이터 폴더의 전용 파일(`liner_api_key`, 본인만 읽기)에만 저장한다. `.env` 파일에서는 키를 읽지도 쓰지도 않는다.
우선순위: 환경 변수 LINER_API_KEY > 화면에서 저장한 키.
키 값은 로그·화면에 그대로 내보내지 않는다.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


KEY_NAME = "LINER_API_KEY"
_KEY_RE = re.compile(r"[\x21-\x7e]{8,512}")  # 공백·줄바꿈 없는 인쇄 가능한 ASCII

SOURCE_LABEL = {
    "env": "환경 변수 LINER_API_KEY",
    "app": "화면에서 저장한 키",
    "none": "설정 안 됨",
}


@dataclass(frozen=True)
class KeyStatus:
    key: str
    source: str  # env | app | none
    app_key_saved: bool  # 화면에서 저장한 키가 있는가 (환경 변수에 가려져 있어도)

    @property
    def label(self) -> str:
        return SOURCE_LABEL[self.source]

    @property
    def masked(self) -> str:
        return mask(self.key)


def mask(key: str) -> str:
    return f"…{key[-4:]}" if len(key) >= 8 else ("설정됨" if key else "")


def validate(raw: str) -> str:
    key = raw.strip()
    if not _KEY_RE.fullmatch(key):
        raise ValueError("키 형식이 올바르지 않습니다. 공백 없이 8~512자로 입력하세요.")
    return key


KEY_FILE_NAME = "liner_api_key"


def key_file(data_dir: Path) -> Path:
    return data_dir / KEY_FILE_NAME


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except (OSError, UnicodeDecodeError):
        return ""


def status(path: Path) -> KeyStatus:
    app_key = _read(path)
    env_key = (os.environ.get(KEY_NAME) or "").strip()
    if env_key:
        return KeyStatus(env_key, "env", bool(app_key))
    if app_key:
        return KeyStatus(app_key, "app", True)
    return KeyStatus("", "none", False)


def write(path: Path, key: str | None) -> None:
    """키를 전용 파일에 저장한다(본인만 읽기, 원자적 교체). None이면 파일을 지운다."""
    if not key:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".liner-key-", dir=path.parent)  # 0600으로 만들어진다
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(key + "\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
