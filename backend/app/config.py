from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "development"
    log_level: str = "INFO"

    backend_host: str = "0.0.0.0"
    backend_port: int = 8000

    database_url: str = "sqlite:///./data/ivc.db"

    jwt_secret: str = Field(
        default="dev-only-secret-do-not-use-in-production-32chars",
        min_length=32,
    )
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720

    default_admin_user: str = "admin"
    default_admin_password: str = "change-me-please"
    default_researcher_user: str = "researcher"
    default_researcher_password: str = "change-me-please"

    # Explicit origin allowlist — no wildcards. Includes both Vercel host and
    # local dev. Extend via the CORS_ORIGINS env var (comma-separated).
    cors_origins: List[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "https://ivc-cage-dashboard.vercel.app",
    ]

    # Host header allowlist used by TrustedHostMiddleware in non-dev environments.
    # Wildcard subdomains like "*.vercel.app" are supported.
    trusted_hosts: List[str] = [
        "industryprojectfinal-production.up.railway.app",
        "*.up.railway.app",
        "*.vercel.app",
        "localhost",
        "127.0.0.1",
    ]

    aggregator_window_seconds: int = 60
    aggregator_tick_seconds: int = 10

    # Camera behavioral-state persistence (for the camera-driven behaviour reports).
    # The detections push is throttled to one persisted sample per camera per this
    # interval, so COUNT(samples) * interval == seconds-in-state (exact, unit-correct).
    camera_sample_interval_s: float = 1.0
    # cam_id -> cage_id attribution (camera states roll up to a cage's report).
    # Unmapped cameras fall back to default_camera_cage.
    camera_cage_map: dict[str, str] = {}
    default_camera_cage: str = "cage-001"

    # Rate limits (slowapi syntax)
    rate_limit_login: str = "10/minute"
    rate_limit_ingest: str = "600/minute"

    @property
    def is_dev(self) -> bool:
        return self.env == "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
