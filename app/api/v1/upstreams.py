import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_admin
from app.db.session import get_session
from app.repositories import routing as routing_repo
from app.schemas.routing import (
    EnabledUpdate,
    RouteCreate,
    RouteOut,
    UpstreamCreate,
    UpstreamOut,
)
from app.services import routing
from app.services.errors import ConflictError, NotFoundError

router = APIRouter(tags=["upstreams and routes"], dependencies=[Depends(require_admin)])


@router.post("/upstreams", response_model=UpstreamOut, status_code=status.HTTP_201_CREATED)
async def create_upstream(body: UpstreamCreate, session: AsyncSession = Depends(get_session)):
    try:
        return await routing.create_upstream(session, body)
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.get("/upstreams", response_model=list[UpstreamOut])
async def list_upstreams(session: AsyncSession = Depends(get_session)):
    return await routing_repo.list_upstreams(session)


@router.patch("/upstreams/{upstream_id}", response_model=UpstreamOut)
async def update_upstream(
    upstream_id: uuid.UUID, body: EnabledUpdate, session: AsyncSession = Depends(get_session)
):
    try:
        return await routing.set_upstream_enabled(session, upstream_id, body.enabled)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


@router.post("/routes", response_model=RouteOut, status_code=status.HTTP_201_CREATED)
async def create_route(body: RouteCreate, session: AsyncSession = Depends(get_session)):
    try:
        return await routing.create_route(session, body)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.get("/routes", response_model=list[RouteOut])
async def list_routes(session: AsyncSession = Depends(get_session)):
    return await routing_repo.list_routes(session)


@router.patch("/routes/{route_id}", response_model=RouteOut)
async def update_route(
    route_id: uuid.UUID, body: EnabledUpdate, session: AsyncSession = Depends(get_session)
):
    try:
        return await routing.set_route_enabled(session, route_id, body.enabled)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
