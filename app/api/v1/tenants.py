import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_admin
from app.db.session import get_session
from app.repositories import tenants as tenant_repo
from app.schemas.tenants import PlanAssign, TenantCreate, TenantOut, TenantPlanOut
from app.services import policy
from app.services.errors import ConflictError, NotFoundError

router = APIRouter(tags=["tenants"], dependencies=[Depends(require_admin)])


@router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(body: TenantCreate, session: AsyncSession = Depends(get_session)):
    try:
        return await policy.create_tenant(session, body.name)
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(session: AsyncSession = Depends(get_session)):
    return await tenant_repo.list_tenants(session)


@router.put("/tenants/{tenant_id}/plan", response_model=TenantPlanOut)
async def assign_plan(
    tenant_id: uuid.UUID, body: PlanAssign, session: AsyncSession = Depends(get_session)
):
    try:
        return await policy.assign_plan(session, tenant_id, body.plan_id)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
