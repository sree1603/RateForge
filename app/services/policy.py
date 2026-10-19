import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Plan, Tenant, TenantPlan
from app.repositories import tenants as tenant_repo
from app.schemas.tenants import PlanCreate
from app.services.errors import ConflictError, NotFoundError


async def create_tenant(session: AsyncSession, name: str) -> Tenant:
    tenant = Tenant(name=name)
    session.add(tenant)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ConflictError(f"A tenant named '{name}' already exists") from None
    await session.refresh(tenant)
    return tenant


async def create_plan(session: AsyncSession, data: PlanCreate) -> Plan:
    plan = Plan(**data.model_dump())
    session.add(plan)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ConflictError(f"A plan named '{data.name}' already exists") from None
    await session.refresh(plan)
    return plan


async def assign_plan(
    session: AsyncSession, tenant_id: uuid.UUID, plan_id: uuid.UUID
) -> TenantPlan:
    """Make `plan_id` the tenant's current plan.

    The tenant row is locked for the duration, so two simultaneous assignments queue up
    instead of both closing the same row and leaving overlapping periods.
    """
    tenant = await tenant_repo.get_tenant(session, tenant_id, lock=True)
    if tenant is None:
        raise NotFoundError("Tenant not found")
    if await tenant_repo.get_plan(session, plan_id) is None:
        raise NotFoundError("Plan not found")

    now = datetime.now(UTC)
    current = await tenant_repo.get_active_tenant_plan(session, tenant_id, now)
    if current is not None:
        if current.plan_id == plan_id:
            return current  # already on this plan; nothing to do
        current.effective_until = now

    assignment = TenantPlan(tenant_id=tenant_id, plan_id=plan_id, effective_from=now)
    session.add(assignment)
    await session.commit()
    await session.refresh(assignment)
    return assignment
