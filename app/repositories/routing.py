import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Route, Upstream


async def get_upstream(session: AsyncSession, upstream_id: uuid.UUID) -> Upstream | None:
    return await session.get(Upstream, upstream_id)


async def list_upstreams(session: AsyncSession) -> list[Upstream]:
    result = await session.execute(select(Upstream).order_by(Upstream.created_at, Upstream.name))
    return list(result.scalars())


async def get_upstream_by_name(session: AsyncSession, name: str) -> Upstream | None:
    result = await session.execute(select(Upstream).where(Upstream.name == name))
    return result.scalar_one_or_none()


async def get_route(session: AsyncSession, route_id: uuid.UUID) -> Route | None:
    return await session.get(Route, route_id)


async def get_route_by_prefix(session: AsyncSession, prefix: str) -> Route | None:
    result = await session.execute(select(Route).where(Route.path_prefix == prefix))
    return result.scalar_one_or_none()


async def list_routes(session: AsyncSession) -> list[Route]:
    result = await session.execute(select(Route).order_by(Route.path_prefix))
    return list(result.scalars())
