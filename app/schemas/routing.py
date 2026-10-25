import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.ssrf import validate_upstream_url
from app.schemas.common import Name

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
ROUTE_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class UpstreamCreate(BaseModel):
    name: Name
    base_url: str = Field(max_length=2048)
    connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    read_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    @field_validator("base_url")
    @classmethod
    def check_url(cls, value: str) -> str:
        return validate_upstream_url(value)


class UpstreamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    base_url: str
    connect_timeout_seconds: float
    read_timeout_seconds: float
    enabled: bool
    created_at: datetime


class RouteCreate(BaseModel):
    upstream_id: uuid.UUID
    path_prefix: str = Field(
        description="Lowercase letters, digits, - and _. Clients call /gateway/<prefix>/..."
    )
    allowed_methods: list[str] = Field(default_factory=lambda: ["GET"], min_length=1)
    strip_prefix: bool = True

    @field_validator("path_prefix")
    @classmethod
    def check_prefix(cls, value: str) -> str:
        if not ROUTE_PREFIX_RE.match(value):
            raise ValueError("path_prefix must match ^[a-z0-9][a-z0-9_-]{0,63}$")
        return value

    @field_validator("allowed_methods")
    @classmethod
    def check_methods(cls, value: list[str]) -> list[str]:
        methods = list(dict.fromkeys(m.upper() for m in value))  # uppercase, de-duplicate
        unknown = set(methods) - ALLOWED_METHODS
        if unknown:
            raise ValueError(f"Unsupported methods: {sorted(unknown)}")
        return methods


class RouteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    upstream_id: uuid.UUID
    path_prefix: str
    allowed_methods: list[str]
    strip_prefix: bool
    enabled: bool
    created_at: datetime


class EnabledUpdate(BaseModel):
    enabled: bool
