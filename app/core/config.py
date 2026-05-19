from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 명백히 안전하지 않은 JWT secret 값들 — 운영(prod) 진입 시 즉시 실패.
_INSECURE_JWT_SECRETS: frozenset[str] = frozenset(
    {
        "",
        "change-me",
        "change-me-use-openssl-rand-hex-32",
        "dev-secret-change-me",
        "secret",
        "test",
    }
)

# env 값 정규화 — 운영으로 인식할 토큰들.
_PROD_ENV_TOKENS: frozenset[str] = frozenset({"prod", "production", "live"})
_DEV_ENV_TOKENS: frozenset[str] = frozenset({"dev", "development", "local"})
_TEST_ENV_TOKENS: frozenset[str] = frozenset({"test", "testing", "ci"})


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

    # 운영자 / cron 전용 API 키. POST /v1/risk/sync/*, /v1/prices/sync*, /v1/macro/sync/*,
    # /v1/tickers/sync/* 같은 sync 라우트가 X-Operator-Key 헤더로 이 값을 검증한다.
    # 일반 사용자 JWT와는 별개 — 운영자만 가지는 정적 시크릿.
    # 생성: `openssl rand -hex 32`
    # GitHub Actions cron이 secrets.OPERATOR_API_KEY로 헤더에 주입한다.
    operator_api_key: str = ""

    # Firebase Cloud Messaging — 웹/iOS/Android 푸시 발송.
    # 둘 중 하나로 서비스 계정 키 주입:
    #   FIREBASE_SERVICE_ACCOUNT_JSON: JSON 통째 (한 줄로 escape 된 형태)
    #   FIREBASE_SERVICE_ACCOUNT_PATH: 파일 경로 (gitignored)
    # 둘 다 비어있으면 FCM 초기화 안 함 → 발송 호출 시 503 INTERNAL_ERROR.
    firebase_project_id: str = ""
    firebase_service_account_json: str = ""
    firebase_service_account_path: str = ""
    # 발송 HTTP timeout (초). FCM 응답이 느릴 때 한 사용자가 다른 요청을 막지 않도록 짧게.
    fcm_send_timeout_sec: int = 10

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
        return self.env.lower() in _DEV_ENV_TOKENS

    @property
    def is_prod(self) -> bool:
        return self.env.lower() in _PROD_ENV_TOKENS

    @property
    def is_test(self) -> bool:
        return self.env.lower() in _TEST_ENV_TOKENS

    @property
    def is_fcm_configured(self) -> bool:
        """FCM 어댑터가 부팅 가능한 상태인가."""
        return bool(
            self.firebase_service_account_json.strip() or self.firebase_service_account_path.strip()
        )

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        """운영 환경(prod/production/live) 진입 시 안전성 검증.

        Railway/EC2 같은 운영 환경에서 JWT_SECRET 미설정 시 토큰 위조 가능.
        CORS 와일드카드는 credentials=true 와 결합 시 인증 우회로 이어짐.
        부팅 단계에서 즉시 실패시켜 사고를 막는다.
        """
        if not self.is_prod:
            return self

        # JWT_SECRET 검증
        if self.jwt_secret.strip() in _INSECURE_JWT_SECRETS:
            raise RuntimeError(
                f"JWT_SECRET 가 운영 환경(env={self.env})에서 기본/취약 값으로 설정되어 있습니다. "
                "최소 32바이트 random hex 로 갱신하세요: `openssl rand -hex 32`"
            )
        if len(self.jwt_secret) < 32:
            raise RuntimeError(
                f"JWT_SECRET 길이가 짧습니다 (>=32 권장, 현재 {len(self.jwt_secret)}). "
                "운영 환경에선 충분한 엔트로피가 필요합니다."
            )

        # CORS_ORIGINS 검증
        origins = self.cors_origins_list
        if not origins:
            raise RuntimeError(
                "CORS_ORIGINS 가 비어있습니다. 운영 환경의 프런트 도메인을 명시하세요."
            )
        if "*" in origins:
            raise RuntimeError(
                "CORS_ORIGINS=* 는 credentials=true 와 결합 시 인증 우회를 허용합니다. "
                "운영에선 명시적 도메인 화이트리스트만 사용하세요."
            )
        # localhost 만 있는 경우도 의심 — 운영인데 dev URL 로 부팅하려는 사고 차단
        if all("localhost" in o or "127.0.0.1" in o for o in origins):
            raise RuntimeError(
                f"CORS_ORIGINS 가 localhost/127.0.0.1 만 포함합니다(현재: {origins}). "
                "운영의 프런트 도메인을 추가하세요."
            )

        # OPERATOR_API_KEY 검증 — 운영의 /sync/* 가 무방비로 노출되는 사고 차단.
        # dev 에서는 빈 값도 허용 (단 require_operator 가 503 으로 거절해 호출 자체 차단).
        op_key = self.operator_api_key.strip()
        if not op_key:
            raise RuntimeError(
                "OPERATOR_API_KEY 가 비어있습니다. 운영 환경의 /sync/* 라우트가 무방비로 "
                "노출되지 않도록 32바이트 이상 random 값을 설정하세요: `openssl rand -hex 32`"
            )
        if len(op_key) < 32:
            raise RuntimeError(
                f"OPERATOR_API_KEY 길이가 짧습니다 (>=32 권장, 현재 {len(op_key)}). "
                "충분한 엔트로피의 값을 사용하세요: `openssl rand -hex 32`"
            )
        if op_key == self.jwt_secret:
            raise RuntimeError("OPERATOR_API_KEY 와 JWT_SECRET 가 동일합니다. 시크릿을 분리하세요.")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
