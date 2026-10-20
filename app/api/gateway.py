import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import Principal, get_tenant_principal
from app.core.config import get_settings
from app.db.session import get_session
from app.repositories import routing as routing_repo
from app.services import routing
from app.services.proxy import ProxyTarget, forward_request

logger = logging.getLogger("rateforge.gateway")
router = APIRouter(prefix="/gateway", tags=["gateway"])

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _json_error(
    status_code: int, detail: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail}, headers=headers)


@router.api_route("/{route_prefix}", methods=METHODS, include_in_schema=False)
@router.api_route("/{route_prefix}/{path:path}", methods=METHODS, include_in_schema=False)
async def gateway(
    request: Request,
    route_prefix: str,
    path: str = "",
    principal: Principal = Depends(get_tenant_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    route = await routing_repo.get_route_by_prefix(session, route_prefix)
    # One answer for "no such route", "route disabled" and "upstream disabled".
    if route is None or not route.enabled or not route.upstream.enabled:
        return _json_error(404, "Route not found")

    method = request.method
    allowed = method in route.allowed_methods or (
        method == "HEAD" and "GET" in route.allowed_methods
    )
    if not allowed:
        return _json_error(
            405, "Method not allowed", {"Allow": ", ".join(sorted(route.allowed_methods))}
        )

    try:
        routing.validate_gateway_path(path, request.scope.get("raw_path"))
    except routing.InvalidPath as exc:
        return _json_error(400, str(exc))

    target = ProxyTarget(
        url=routing.build_upstream_url(
            route.upstream.base_url, route.path_prefix, route.strip_prefix, path
        ),
        route_prefix=route.path_prefix,
        tenant_id=str(principal.tenant_id),
        connect_timeout=route.upstream.connect_timeout_seconds,
        read_timeout=route.upstream.read_timeout_seconds,
    )

    # Give the database connection back to the pool *before* the (possibly slow) upstream
    # call. Otherwise every in-flight proxied request would hold a connection.
    await session.close()

    settings = get_settings()
    return await forward_request(
        request,
        target,
        client=request.app.state.http_client,
        max_body_bytes=settings.gateway_max_body_bytes,
        pool_timeout=settings.proxy_pool_timeout_seconds,
    )
