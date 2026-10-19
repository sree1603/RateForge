from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_admin
from app.db.session import get_session
from app.repositories import tenants as tenant_repo
from app.schemas.tenants import PlanCreate, PlanOut
from app.services import policy
from app.services.errors import ConflictError

router = APIRouter(tags=["plans"], dependencies=[Depends(require_admin)])


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(body: PlanCreate, session: AsyncSession = Depends(get_session)):
    try:
        return await policy.create_plan(session, body)
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(session: AsyncSession = Depends(get_session)):
    return await tenant_repo.list_plans(session)
