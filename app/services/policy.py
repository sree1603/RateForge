import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
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
    policy_cache.invalidate(tenant_id)  # this process sees the change immediately
    return assignment


@dataclass(frozen=True)
class ResolvedPolicy:
    """What the rate limiter needs to know about a tenant's current plan."""

    plan_name: str
    requests_per_window: int
    window_seconds: int
    monthly_quota: int | None


class PolicyCache:
    """Small in-process cache of tenant -> current policy, so the limiter does not need a
    plan query on every request. Entries expire after `policy_cache_ttl_seconds`.

    Trade-off: with several gateway instances, a plan change made through one instance is
    seen by the others only when their cached entry expires (at most the TTL).
    "No plan" results are cached too, so an unplanned tenant cannot be used to hammer the DB.
    """

    MAX_ENTRIES = 10_000

    def __init__(self) -> None:
        self._entries: dict[uuid.UUID, tuple[float, ResolvedPolicy | None]] = {}

    def get(self, tenant_id: uuid.UUID) -> tuple[bool, ResolvedPolicy | None]:
        entry = self._entries.get(tenant_id)
        if entry is None or entry[0] <= time.monotonic():
            self._entries.pop(tenant_id, None)
            return False, None
        return True, entry[1]

    def put(self, tenant_id: uuid.UUID, policy: ResolvedPolicy | None) -> None:
        if len(self._entries) >= self.MAX_ENTRIES:
            self._entries.clear()
        ttl = get_settings().policy_cache_ttl_seconds
        self._entries[tenant_id] = (time.monotonic() + ttl, policy)

    def invalidate(self, tenant_id: uuid.UUID) -> None:
        self._entries.pop(tenant_id, None)

    def clear(self) -> None:
        self._entries.clear()


policy_cache = PolicyCache()


async def get_policy(session: AsyncSession, tenant_id: uuid.UUID) -> ResolvedPolicy | None:
    """The tenant's current policy, or None if no plan is assigned."""
    hit, cached = policy_cache.get(tenant_id)
    if hit:
        return cached
    assignment = await tenant_repo.get_active_tenant_plan(session, tenant_id, datetime.now(UTC))
    policy = (
        ResolvedPolicy(
            plan_name=assignment.plan.name,
            requests_per_window=assignment.plan.requests_per_window,
            window_seconds=assignment.plan.window_seconds,
            monthly_quota=assignment.plan.monthly_quota,
        )
        if assignment
        else None
    )
    policy_cache.put(tenant_id, policy)
    return policy
