import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey


async def get_key(session: AsyncSession, key_id: uuid.UUID) -> ApiKey | None:
    return await session.get(ApiKey, key_id)


async def get_key_by_prefix(session: AsyncSession, prefix: str) -> ApiKey | None:
    result = await session.execute(select(ApiKey).where(ApiKey.key_prefix == prefix))
    return result.scalar_one_or_none()


async def list_keys(session: AsyncSession, tenant_id: uuid.UUID | None = None) -> list[ApiKey]:
    stmt = select(ApiKey).order_by(ApiKey.created_at.desc())
    if tenant_id is not None:
        stmt = stmt.where(ApiKey.tenant_id == tenant_id)
    return list((await session.execute(stmt)).scalars())
