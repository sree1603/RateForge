import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Name


class KeyCreate(BaseModel):
    # Admins must say which tenant; tenants may omit it (it can only be themselves).
    tenant_id: uuid.UUID | None = None
    name: Name
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class KeyOut(BaseModel):
    """Key metadata. Deliberately has no field that could hold the secret or its hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    key_prefix: str
    status: str
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime


class KeyCreated(KeyOut):
    api_key: str = Field(description="The full key. Shown once; store it now.")
