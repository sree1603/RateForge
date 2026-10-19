from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import constant_time_equals, hash_api_key, parse_key_prefix
from app.models import ApiKey
from app.models.enums import KeyStatus, TenantStatus
from app.repositories import keys as key_repo

# Hash compared against when the prefix is unknown, so "no such key" and
# "wrong secret" take about the same time.
_DUMMY_HASH = "0" * 64


class InvalidCredentials(Exception):
    """Unknown, malformed, wrong, revoked or expired key. Deliberately not more specific."""


class TenantSuspended(Exception):
    pass


async def authenticate_api_key(session: AsyncSession, raw_key: str) -> ApiKey:
    prefix = parse_key_prefix(raw_key)
    presented_hash = hash_api_key(raw_key)  # always computed, even for malformed keys

    key = await key_repo.get_key_by_prefix(session, prefix) if prefix else None
    stored_hash = key.key_hash if key else _DUMMY_HASH
    hash_matches = constant_time_equals(presented_hash, stored_hash)

    if key is None or not hash_matches:
        raise InvalidCredentials
    if key.status != KeyStatus.ACTIVE.value:
        raise InvalidCredentials

    now = datetime.now(UTC)
    if key.expires_at is not None and key.expires_at <= now:
        raise InvalidCredentials
    if key.tenant.status != TenantStatus.ACTIVE.value:
        raise TenantSuspended

    await _touch_last_used(session, key, now)
    return key


async def _touch_last_used(session: AsyncSession, key: ApiKey, now: datetime) -> None:
    gap = timedelta(seconds=get_settings().key_last_used_update_seconds)
    if key.last_used_at is None or now - key.last_used_at >= gap:
        key.last_used_at = now
        await session.commit()
