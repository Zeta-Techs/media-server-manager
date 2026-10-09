from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration shared by API, workers and migration tools."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="MSM_", extra="ignore")

    app_name: str = "Media Server Manager"
    environment: str = "development"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./config/media_server_manager_modular.db"
    redis_url: str = "redis://localhost:6379/0"
    oidc_issuer: str = ""
    oidc_audience: str = "media-server-manager"
    oidc_jwks_url: str = ""
    jwt_secret: str = Field(default="development-only-change-me", repr=False)
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 2_592_000
    cors_origins: str = "http://localhost:5173"
    sql_echo: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
