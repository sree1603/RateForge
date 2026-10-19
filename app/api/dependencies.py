import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_admin_token
from app.db.session import get_session
from app.services.authentication import InvalidCredentials, TenantSuspended, authenticate_api_key

_admin_header = APIKeyHeader(name="X-Admin-Token", auto_error=False, scheme_name="AdminToken")
_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, scheme_name="ApiKey")


@dataclass(frozen=True)
class Principal:
    is_admin: bool
    tenant_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None


def _unauthorized() -> HTTPException:
    # One message for every failure, so callers cannot tell "unknown key" from "revoked key".
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing credentials")


async def get_principal(
    admin_token: str | None = Depends(_admin_header),
    api_key: str | None = Depends(_key_header),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    if admin_token is not None:
        if verify_admin_token(admin_token):
            return Principal(is_admin=True)
        raise _unauthorized()
    if api_key:
        try:
            key = await authenticate_api_key(session, api_key)
        except InvalidCredentials:
            raise _unauthorized() from None
        except TenantSuspended:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Tenant is suspended") from None
        return Principal(is_admin=False, tenant_id=key.tenant_id, api_key_id=key.id)
    raise _unauthorized()


async def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return principal
