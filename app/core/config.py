from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://rateforge:rateforge@localhost:5432/rateforge"
    redis_url: str = "redis://localhost:6379/0"

    # How long a readiness probe waits for each dependency.
    health_check_timeout_seconds: float = 2.0

    # Secrets: required, no defaults, so a missing value fails at startup instead of
    # silently running with something guessable.
    api_key_hmac_secret: str = Field(min_length=32)
    admin_token: str = Field(min_length=16)

    # Gateway / proxy
    gateway_max_body_bytes: int = 10 * 1024 * 1024
    http_pool_max_connections: int = 200
    http_pool_max_keepalive: int = 50
    proxy_pool_timeout_seconds: float = 5.0
    # If False, upstream URLs pointing at private or loopback addresses are rejected.
    # Leave True when upstreams are internal services (Docker network, VPC).
    allow_private_upstreams: bool = True

    # Rate limiting
    # What to do when Redis cannot be reached: "closed" rejects requests with 503 (protects
    # upstreams, hurts availability); "open" lets them through unlimited (the reverse).
    rate_limit_failure_mode: Literal["open", "closed"] = "closed"
    redis_timeout_seconds: float = 0.5
    redis_max_connections: int = 100
    # How long a tenant's resolved plan is cached in each gateway process. A plan change
    # made on another instance can take up to this long to be noticed.
    policy_cache_ttl_seconds: float = 30.0

    # Minimum gap between `last_used_at` writes for one key (avoids a write per request).
    key_last_used_update_seconds: int = 60

    @model_validator(mode="after")
    def reject_dev_secrets_in_production(self) -> "Settings":
        if self.environment.lower() == "production":
            for name in ("api_key_hmac_secret", "admin_token"):
                if getattr(self, name).startswith("dev-only"):
                    raise ValueError(f"{name} must be changed before running in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
