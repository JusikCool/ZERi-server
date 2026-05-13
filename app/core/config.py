from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: str = "dev"

    database_url: str = "postgresql+asyncpg://before:before@localhost:5432/before"

    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_expires_in: int = 3600
    refresh_token_expires_in: int = 60 * 60 * 24 * 14

    cors_origins: str = "http://localhost:3000"

    fred_api_key: str = ""

    # ZERi-ai-model 폴더 경로 (predict_kronos.py / predict_with_xai.py 위치).
    # 미설정 시 ../ZERi-ai-model 로 자동 추정.
    zeri_ai_model_path: str = ""
    # 추론 스크립트 실행에 쓸 파이썬 인터프리터. 비우면 sys.executable.
    zeri_ai_model_python: str = ""
    # 한 번 호출에 허용할 최대 추론 시간(초). 기본 4시간 (CPU Kronos 50종목 최대).
    inference_timeout_sec: int = 60 * 60 * 4

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
