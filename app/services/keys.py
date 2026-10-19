import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_api_key
from app.models import ApiKey
from app.models.enums import KeyStatus, TenantStatus
from app.repositories import tenants as tenant_repo
from app.services.errors import ConflictError, NotFoundError

logger = logging.getLogger("rateforge.keys")

MAX_PREFIX_RETRIES = 3


async def create_key(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    expires_in_days: int | None,
) -> tuple[ApiKey, str]:
    """Create a key and return (row, raw_key). The raw key exists only in this return value."""
    tenant = await tenant_repo.get_tenant(session, tenant_id)
    if tenant is None:
        raise NotFoundError("Tenant not found")
    if tenant.status != TenantStatus.ACTIVE.value:
        raise ConflictError("Tenant is not active")

    expires_at = datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None

    for _ in range(MAX_PREFIX_RETRIES):
        generated = generate_api_key()
        key = ApiKey(
            tenant_id=tenant_id,
            name=name,
            key_prefix=generated.prefix,
            key_hash=generated.key_hash,
            expires_at=expires_at,
        )
        session.add(key)
        try:
            await session.commit()
        except IntegrityError:
            # Prefix collision (12 hex chars, so very rare): try again with a fresh key.
            await session.rollback()
            continue
        await session.refresh(key)
        logger.info("api key created", extra={"path": f"key_prefix={key.key_prefix}"})
        return key, generated.raw
    raise ConflictError("Could not allocate a unique key; please retry")


async def revoke_key(session: AsyncSession, key: ApiKey) -> None:
    """Idempotent: revoking an already revoked key changes nothing."""
    if key.status == KeyStatus.REVOKED.value:
        return
    key.status = KeyStatus.REVOKED.value
    key.revoked_at = datetime.now(UTC)
    await session.commit()
    logger.info("api key revoked", extra={"path": f"key_prefix={key.key_prefix}"})
