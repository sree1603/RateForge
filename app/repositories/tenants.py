import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Plan, Tenant, TenantPlan


async def get_tenant(
    session: AsyncSession, tenant_id: uuid.UUID, *, lock: bool = False
) -> Tenant | None:
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_tenant_by_name(session: AsyncSession, name: str) -> Tenant | None:
    return (await session.execute(select(Tenant).where(Tenant.name == name))).scalar_one_or_none()


async def list_tenants(session: AsyncSession) -> list[Tenant]:
    result = await session.execute(select(Tenant).order_by(Tenant.created_at, Tenant.name))
    return list(result.scalars())


async def get_plan(session: AsyncSession, plan_id: uuid.UUID) -> Plan | None:
    return await session.get(Plan, plan_id)


async def get_plan_by_name(session: AsyncSession, name: str) -> Plan | None:
    return (await session.execute(select(Plan).where(Plan.name == name))).scalar_one_or_none()


async def list_plans(session: AsyncSession) -> list[Plan]:
    result = await session.execute(select(Plan).order_by(Plan.requests_per_window, Plan.name))
    return list(result.scalars())


async def get_active_tenant_plan(
    session: AsyncSession, tenant_id: uuid.UUID, at: datetime
) -> TenantPlan | None:
    """The plan assignment in force at `at`. Used by the rate limiter in Phase 4."""
    stmt = (
        select(TenantPlan)
        .where(
            TenantPlan.tenant_id == tenant_id,
            TenantPlan.effective_from <= at,
            (TenantPlan.effective_until.is_(None)) | (TenantPlan.effective_until > at),
        )
        .order_by(TenantPlan.effective_from.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
