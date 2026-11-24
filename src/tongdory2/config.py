"""설정 로드 (.env → Settings). 키는 코드에 두지 않는다."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_HOSTNAME = "tongdory.localhost"


def is_compiled() -> bool:
    """Nuitka 배포본으로 실행 중인가 (Nuitka가 컴파일된 모듈에 `__compiled__`를 넣는다)."""
    return "__compiled__" in globals()


def env_file_path(compiled: bool | None = None) -> Path | None:
    """소스 실행에서 직접 편집하는 `.env`(포트 등 실행 설정). 배포본(.app/exe)에는 없다.

    LINER_API_KEY는 이 파일에서 읽지 않는다. 키는 화면 [Liner API 키] 또는 환경 변수로만 설정한다.
    """
    if is_compiled() if compiled is None else compiled:
        return None
    return PROJECT_ROOT / ".env"


def _default_app_data() -> Path:
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "Tongdory2"
    return Path.home() / "Library" / "Application Support" / "Tongdory2"


class Settings(BaseSettings):
    # .env 파일은 get_settings()에서만 읽는다. 직접 만든 Settings(...)는 환경 변수와 인자만 쓴다.
    model_config = SettingsConfigDict(env_file=None, env_file_encoding="utf-8", extra="ignore")

    liner_api_key: str = Field(default="", alias="LINER_API_KEY")
    liner_base_url: str = Field(default="https://platform.liner.com/api/v1", alias="LINER_BASE_URL")

    # 데모에서는 가짜 홈(demo/home)을 가리킨다. 비어 있으면 실제 홈.
    tongdory_home: str = Field(default="", alias="TONGDORY_HOME")
    app_data_dir: str = Field(default="", alias="TONGDORY_DATA_DIR")

    recent_hours: float = Field(default=2.0, alias="RECENT_HOURS")  # 결정 #9
    batch_size: int | None = Field(default=None, ge=1, le=10000, alias="TONGDORY_BATCH_SIZE")
    host: str = "127.0.0.1"
    port: int = Field(default=8765, alias="TONGDORY_PORT")
    # 브라우저 주소에 보이는 이름. *.localhost는 브라우저·macOS가 항상 이 PC(루프백)로 연결한다.
    browser_host: str = Field(default=APP_HOSTNAME, alias="TONGDORY_BROWSER_HOST")

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        """`.env` 파일에서는 LINER_API_KEY를 읽지 않는다 (키는 화면 또는 환경 변수로만)."""
        def dotenv_without_key():
            values = dotenv_settings()
            return {k: v for k, v in values.items() if k.lower() not in ("liner_api_key",)}
        return init_settings, env_settings, dotenv_without_key, file_secret_settings

    @property
    def allowed_hosts(self) -> list[str]:
        """앱이 받는 주소 이름. 이 PC를 가리키는 이름만 허용해 외부 도메인 우회(DNS rebinding)를 막는다."""
        return list(dict.fromkeys(["127.0.0.1", "localhost", APP_HOSTNAME, self.app_host]))

    @property
    def app_host(self) -> str:
        h = self.browser_host.strip().lower()
        return h if h in ("127.0.0.1", "localhost") or h.endswith(".localhost") else APP_HOSTNAME

    @property
    def app_url(self) -> str:
        return f"http://{self.app_host}:{self.port}/"

    @property
    def fallback_url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def home(self) -> Path:
        return Path(self.tongdory_home).expanduser() if self.tongdory_home else Path.home()

    @property
    def data_dir(self) -> Path:
        return Path(self.app_data_dir).expanduser() if self.app_data_dir else _default_app_data()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tongdory2.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    from tongdory2.liner import keystore

    settings = Settings(_env_file=env_file_path())
    # 키는 환경 변수 또는 화면에서 저장한 키만 쓴다 (.env 파일의 키는 읽지 않는다).
    settings.liner_api_key = keystore.status(keystore.key_file(settings.data_dir)).key
    return settings


def reset_settings_cache() -> None:
    get_settings.cache_clear()
    os.environ.pop("_TONGDORY_SETTINGS_CACHED", None)
