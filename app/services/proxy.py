import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.core.logging import request_id_ctx
from app.observability.metrics import UPSTREAM_ERRORS

logger = logging.getLogger("rateforge.proxy")

# RFC 9110 hop-by-hop headers: meaningful for one connection only, never forwarded.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Headers the gateway owns. Anything the client sends under these names is dropped, then
# the gateway sets its own, so clients cannot spoof them.
GATEWAY_OWNED_REQUEST_HEADERS = frozenset(
    {
        "host",
        "x-api-key",
        "x-admin-token",
        "x-request-id",
        "x-forwarded-for",
        "x-rateforge-tenant",
    }
)


@dataclass(frozen=True)
class ProxyTarget:
    url: str
    route_prefix: str
    tenant_id: str
    connect_timeout: float
    read_timeout: float


class BodyTooLarge(Exception):
    pass


class _LimitedBody:
    """Wraps the incoming body stream and aborts once it exceeds the limit."""

    def __init__(self, stream: AsyncIterator[bytes], limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self.exceeded = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        total = 0
        async for chunk in self._stream:
            total += len(chunk)
            if total > self._limit:
                self.exceeded = True
                raise BodyTooLarge
            yield chunk


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _connection_tokens(raw_headers: list[tuple[bytes, bytes]]) -> set[str]:
    """Header names listed in `Connection: x, y` are hop-by-hop for this request too."""
    tokens: set[str] = set()
    for name, value in raw_headers:
        if name.lower() == b"connection":
            tokens.update(
                t.strip().lower() for t in value.decode("latin-1").split(",") if t.strip()
            )
    return tokens


def filter_request_headers(
    raw_headers: list[tuple[bytes, bytes]],
    *,
    request_id: str | None,
    tenant_id: str,
    client_ip: str | None,
) -> list[tuple[str, str]]:
    drop = HOP_BY_HOP | GATEWAY_OWNED_REQUEST_HEADERS | _connection_tokens(raw_headers)
    forwarded_for = [
        value.decode("latin-1") for name, value in raw_headers if name.lower() == b"x-forwarded-for"
    ]
    has_accept_encoding = False
    out: list[tuple[str, str]] = []
    for name, value in raw_headers:
        lowered = name.decode("latin-1").lower()
        if lowered == "accept-encoding":
            has_accept_encoding = True
        if lowered in drop:
            continue
        out.append((lowered, value.decode("latin-1")))

    if not has_accept_encoding:
        # httpx would otherwise ask for gzip on its own, and we pass response bytes through
        # untouched, so a client that never asked for compression could receive it.
        out.append(("accept-encoding", "identity"))
    chain = [*forwarded_for, client_ip] if client_ip else forwarded_for
    if chain:
        out.append(("x-forwarded-for", ", ".join(chain)))
    if request_id:
        out.append(("x-request-id", request_id))
    out.append(("x-rateforge-tenant", tenant_id))
    return out


def filter_response_headers(raw_headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    drop = HOP_BY_HOP | _connection_tokens(raw_headers)
    return [(n.lower(), v) for n, v in raw_headers if n.decode("latin-1").lower() not in drop]


def _has_body(headers: list[tuple[bytes, bytes]]) -> bool:
    names = {n.lower(): v for n, v in headers}
    if b"transfer-encoding" in names:
        return True
    try:
        return int(names.get(b"content-length", b"0")) > 0
    except ValueError:
        return False


async def forward_request(
    request: Request,
    target: ProxyTarget,
    client: httpx.AsyncClient,
    max_body_bytes: int,
    pool_timeout: float,
) -> Response:
    """Send the request to the upstream and stream its response back.

    Failures become predictable gateway errors (413, 502, 503, 504). There are no
    automatic retries: retrying a non-idempotent request could repeat its side effects.
    """
    raw_headers = request.headers.raw
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            return _error(400, "Invalid Content-Length header")
        if declared < 0:
            return _error(400, "Invalid Content-Length header")
        if declared > max_body_bytes:
            return _error(413, "Request body too large")

    headers = filter_request_headers(
        raw_headers,
        request_id=request_id_ctx.get(),
        tenant_id=target.tenant_id,
        client_ip=request.client.host if request.client else None,
    )

    limiter: _LimitedBody | None = None
    if _has_body(raw_headers):
        limiter = _LimitedBody(request.stream(), max_body_bytes)

    timeout = httpx.Timeout(
        connect=target.connect_timeout,
        read=target.read_timeout,
        write=target.read_timeout,
        pool=pool_timeout,
    )
    try:
        upstream_request = client.build_request(
            request.method,
            target.url + (f"?{request.url.query}" if request.url.query else ""),
            headers=headers,
            content=limiter,
            timeout=timeout,
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.InvalidURL:
        return _error(400, "Invalid request URL")
    except httpx.PoolTimeout:
        UPSTREAM_ERRORS.labels(kind="pool_exhausted").inc()
        logger.warning("proxy pool exhausted", extra={"path": target.route_prefix})
        return _error(503, "Gateway is overloaded; try again shortly")
    except httpx.TimeoutException:
        UPSTREAM_ERRORS.labels(kind="timeout").inc()
        logger.warning("upstream timeout", extra={"path": target.route_prefix})
        return _error(504, "Upstream timed out")
    except (httpx.TransportError, BodyTooLarge) as exc:
        if limiter is not None and limiter.exceeded:
            UPSTREAM_ERRORS.labels(kind="body_too_large").inc()
            return _error(413, "Request body too large")
        UPSTREAM_ERRORS.labels(kind="unreachable").inc()
        logger.warning(
            "upstream error", extra={"path": f"{target.route_prefix}:{type(exc).__name__}"}
        )
        return _error(502, "Upstream is unreachable or returned an invalid response")

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.HTTPError as exc:
            # Headers are already sent, so the status cannot change; end the stream early.
            logger.warning(
                "upstream failed mid-response",
                extra={"path": f"{target.route_prefix}:{type(exc).__name__}"},
            )
        finally:
            await upstream.aclose()

    response = StreamingResponse(body(), status_code=upstream.status_code)
    response.raw_headers = filter_response_headers(upstream.headers.raw)
    return response
