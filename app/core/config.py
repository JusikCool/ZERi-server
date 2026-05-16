from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# 명백히 안전하지 않은 JWT secret 값들 — 운영(env != dev) 진입 시 즉시 실패.
_INSECURE_JWT_SECRETS: frozenset[str] = frozenset({
    "",
    "change-me",
    "change-me-use-openssl-rand-hex-32",
    "dev-secret-change-me",
    "secret",
    "test",
})


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

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        """env != dev/test 일 때 기본/취약 시크릿으로 부팅 금지.

        Railway/EC2 같은 운영 환경에서 JWT_SECRET 미설정 시 토큰 위조 가능.
        부팅 단계에서 즉시 실패시켜 사고를 막는다.
        """
        if self.env in {"dev", "test"}:
            return self
        if self.jwt_secret.strip() in _INSECURE_JWT_SECRETS:
            raise ValueError(
                f"JWT_SECRET 가 운영 환경(env={self.env})에서 기본/취약 값으로 설정되어 있습니다. "
                "최소 32바이트 random hex 로 갱신하세요: `openssl rand -hex 32`"
            )
        if len(self.jwt_secret) < 32:
            raise ValueError(
                f"JWT_SECRET 길이가 짧습니다(>=32 권장, 현재 {len(self.jwt_secret)})."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
