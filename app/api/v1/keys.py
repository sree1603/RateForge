import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import Principal, get_principal
from app.db.session import get_session
from app.repositories import keys as key_repo
from app.schemas.keys import KeyCreate, KeyCreated, KeyOut
from app.services import keys as key_service
from app.services.errors import ConflictError, NotFoundError

router = APIRouter(tags=["keys"])


@router.post("/keys", response_model=KeyCreated, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: KeyCreate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
):
    if principal.is_admin:
        if body.tenant_id is None:
            raise HTTPException(422, "tenant_id is required")
        tenant_id = body.tenant_id
    else:
        if body.tenant_id not in (None, principal.tenant_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot create keys for another tenant")
        tenant_id = principal.tenant_id

    try:
        key, raw = await key_service.create_key(
            session, tenant_id=tenant_id, name=body.name, expires_in_days=body.expires_in_days
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    return KeyCreated(**KeyOut.model_validate(key).model_dump(), api_key=raw)


@router.get("/keys", response_model=list[KeyOut])
async def list_keys(
    tenant_id: uuid.UUID | None = None,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
):
    # Tenants only ever see their own keys; the filter is not under their control.
    scope = tenant_id if principal.is_admin else principal.tenant_id
    return await key_repo.list_keys(session, scope)


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    key_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
):
    key = await key_repo.get_key(session, key_id)
    # 404 (not 403) for other tenants' keys, so key IDs cannot be probed.
    if key is None or (not principal.is_admin and key.tenant_id != principal.tenant_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")
    await key_service.revoke_key(session, key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
