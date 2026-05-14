from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 운영(prod)에서 절대 사용하면 안 되는 약한 시크릿 값들.
# .env에 깜빡 잊고 dev default가 그대로 prod에 들어가면 토큰 위조가 가능해지므로
# 부팅 단계에서 실패시킴 — 컨테이너가 unhealthy로 떨어지면서 잘못된 배포를 알 수 있다.
_UNSAFE_JWT_SECRETS: frozenset[str] = frozenset(
    {
        "",
        "change-me",
        "dev-secret-change-me",
        "change-me-use-openssl-rand-hex-32",
        "secret",
        "password",
        "test",
    }
)


def _is_prod_env(env_value: str) -> bool:
    return env_value.lower() in {"prod", "production", "live"}


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

    @property
    def is_prod(self) -> bool:
        return _is_prod_env(self.env)

    @model_validator(mode="after")
    def _enforce_prod_safety(self) -> "Settings":
        """운영(prod) 환경에서 약한 시크릿/와일드카드 CORS 사용을 차단.

        의도적으로 부팅 단계에서 RuntimeError를 던져 컨테이너가 healthy로 떠 잘못 배포되는
        것을 막는다. dev 환경에서는 그대로 통과.
        """
        if not self.is_prod:
            return self

        if self.jwt_secret in _UNSAFE_JWT_SECRETS:
            raise RuntimeError(
                "운영(prod) 환경에서 dev/기본 JWT_SECRET을 사용할 수 없습니다. "
                "환경변수 JWT_SECRET을 `openssl rand -hex 32`로 생성한 값으로 설정하세요."
            )

        if len(self.jwt_secret) < 32:
            raise RuntimeError(
                "운영(prod) 환경의 JWT_SECRET은 최소 32자 이상이어야 합니다. "
                "`openssl rand -hex 32` 권장."
            )

        if "*" in self.cors_origins_list:
            raise RuntimeError(
                "운영(prod) 환경에서 CORS_ORIGINS에 와일드카드(*)를 사용할 수 없습니다."
            )

        if not self.cors_origins_list:
            raise RuntimeError(
                "운영(prod) 환경의 CORS_ORIGINS는 명시적으로 도메인을 지정해야 합니다."
            )

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
