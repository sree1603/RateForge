import asyncio
import logging

import redis
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import Principal, get_tenant_principal
from app.core.config import get_settings
from app.core.usage_context import usage_ctx
from app.db.session import get_session
from app.observability.metrics import LIMITER_FAILURES
from app.schemas.routing import ROUTE_PREFIX_RE
from app.services import policy as policy_service
from app.services import routing
from app.services.proxy import ProxyTarget, forward_request
from app.services.rate_limiter import Decision

logger = logging.getLogger("rateforge.gateway")
router = APIRouter(prefix="/gateway", tags=["gateway"])

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Anything that means "could not get an answer from Redis".
LIMITER_ERRORS = (redis.RedisError, OSError, TimeoutError, asyncio.TimeoutError)


def _limit_headers(decision: Decision) -> dict[str, str]:
    headers = {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_seconds),
    }
    if decision.quota_limit is not None:
        headers["X-Quota-Limit"] = str(decision.quota_limit)
        headers["X-Quota-Remaining"] = str(decision.quota_remaining)
    return headers


def _rejection(decision: Decision) -> JSONResponse:
    if decision.outcome == "quota_exceeded":
        detail = "Monthly quota exceeded"
    else:
        detail = "Rate limit exceeded"
    return _json_error(
        429, detail, {**_limit_headers(decision), "Retry-After": str(decision.retry_after_seconds)}
    )


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
    # Prefixes are validated when routes are created, so anything else cannot match a route.
    # Checking first keeps hostile bytes (NUL, huge strings) away from the database.
    if not ROUTE_PREFIX_RE.fullmatch(route_prefix):
        return _json_error(404, "Route not found")
    route = await routing.get_route_snapshot(session, route_prefix)
    # One answer for "no such route", "route disabled" and "upstream disabled".
    if route is None or not route.active:
        return _json_error(404, "Route not found")

    # From here on the request is attributable to a tenant and a real route, so it is
    # recorded (see app/core/usage_context.py). The outcome starts as "rejected" and is
    # upgraded below as the request gets further.
    usage = usage_ctx.get()
    if usage is not None:
        usage.tenant_id = principal.tenant_id
        usage.route_id = route.route_id
        usage.route_prefix = route.path_prefix

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

    # Every request that would actually be forwarded is checked against the tenant's plan.
    # Malformed or unknown requests (404/405/400 above) cost nothing, so they cannot eat a
    # tenant's quota.
    settings = get_settings()
    policy = await policy_service.get_policy(session, principal.tenant_id)
    if policy is None:
        return _json_error(403, "No plan is assigned to this tenant")

    decision: Decision | None = None
    try:
        decision = await request.app.state.rate_limiter.check(principal.tenant_id, policy)
    except LIMITER_ERRORS as exc:
        LIMITER_FAILURES.inc()
        logger.error(
            "rate limiter unavailable",
            extra={"path": f"{route.path_prefix}:{type(exc).__name__}"},
        )
        if settings.rate_limit_failure_mode == "closed":
            return _json_error(
                503, "Rate limiter unavailable; try again shortly", {"Retry-After": "1"}
            )
        # fail open: carry on without limiting, and without limit headers
    if decision is not None and not decision.allowed:
        if usage is not None:
            usage.outcome = decision.outcome  # "rate_limited" or "quota_exceeded"
        return _rejection(decision)
    if usage is not None:
        usage.outcome = "forwarded"

    target = ProxyTarget(
        url=routing.build_upstream_url(route.base_url, route.path_prefix, route.strip_prefix, path),
        route_prefix=route.path_prefix,
        tenant_id=str(principal.tenant_id),
        connect_timeout=route.connect_timeout_seconds,
        read_timeout=route.read_timeout_seconds,
    )

    # Give the database connection back to the pool *before* the (possibly slow) upstream
    # call. Otherwise every in-flight proxied request would hold a connection.
    await session.close()

    response = await forward_request(
        request,
        target,
        client=request.app.state.http_client,
        max_body_bytes=settings.gateway_max_body_bytes,
        pool_timeout=settings.proxy_pool_timeout_seconds,
    )
    if decision is not None:
        for name, value in _limit_headers(decision).items():
            response.headers[name] = value  # replaces any same-named header from the upstream
    return response
