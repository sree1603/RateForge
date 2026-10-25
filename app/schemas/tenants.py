import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Name


class TenantCreate(BaseModel):
    name: Name


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: str
    created_at: datetime


class PlanCreate(BaseModel):
    name: Name
    requests_per_window: int = Field(gt=0, le=1_000_000)
    window_seconds: int = Field(gt=0, le=86_400)
    monthly_quota: int | None = Field(default=None, gt=0)


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    requests_per_window: int
    window_seconds: int
    monthly_quota: int | None


class PlanAssign(BaseModel):
    plan_id: uuid.UUID


class TenantPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tenant_id: uuid.UUID
    effective_from: datetime
    effective_until: datetime | None
    plan: PlanOut
