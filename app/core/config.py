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

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
