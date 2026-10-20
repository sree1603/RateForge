import logging
import uuid
from urllib.parse import quote, urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Route, Upstream
from app.repositories import routing as routing_repo
from app.schemas.routing import RouteCreate, UpstreamCreate
from app.services.errors import ConflictError, NotFoundError

logger = logging.getLogger("rateforge.routing")

# Characters left unescaped inside one path segment. '/' is deliberately absent.
_SEGMENT_SAFE = "!$&'()*+,;=:@-._~"
_BAD_ENCODED_TOKENS = (b"%2e", b"%2f", b"%5c", b"%00")


class InvalidPath(ValueError):
    pass


# ---------- admin operations ----------


async def create_upstream(session: AsyncSession, data: UpstreamCreate) -> Upstream:
    upstream = Upstream(**data.model_dump())
    session.add(upstream)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ConflictError(f"An upstream named '{data.name}' already exists") from None
    await session.refresh(upstream)
    return upstream


async def create_route(session: AsyncSession, data: RouteCreate) -> Route:
    if await routing_repo.get_upstream(session, data.upstream_id) is None:
        raise NotFoundError("Upstream not found")
    route = Route(**data.model_dump())
    session.add(route)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ConflictError(f"A route with prefix '{data.path_prefix}' already exists") from None
    await session.refresh(route)
    return route


async def set_upstream_enabled(
    session: AsyncSession, upstream_id: uuid.UUID, enabled: bool
) -> Upstream:
    upstream = await routing_repo.get_upstream(session, upstream_id)
    if upstream is None:
        raise NotFoundError("Upstream not found")
    upstream.enabled = enabled
    await session.commit()
    return upstream


async def set_route_enabled(session: AsyncSession, route_id: uuid.UUID, enabled: bool) -> Route:
    route = await routing_repo.get_route(session, route_id)
    if route is None:
        raise NotFoundError("Route not found")
    route.enabled = enabled
    await session.commit()
    return route


# ---------- request path handling ----------


def validate_gateway_path(path: str, raw_path: bytes | None = None) -> None:
    """Reject paths that could escape the route's directory on the upstream.

    `path` is what the framework already percent-decoded; `raw_path` is the bytes the
    client actually sent. Both are checked, because "%2e%2e" decodes to ".." and some
    servers decode a second time.
    """
    if raw_path is not None:
        lowered = raw_path.lower()
        if any(token in lowered for token in _BAD_ENCODED_TOKENS):
            raise InvalidPath("Encoded dots, slashes, backslashes and NUL are not allowed")
    if "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise InvalidPath("Path contains invalid characters")
    if not path:
        return
    segments = path.split("/")
    if any(segment in (".", "..") for segment in segments):
        raise InvalidPath("Path must not contain '.' or '..' segments")
    # A single trailing slash is meaningful (/users/); empty segments elsewhere ("a//b") are not.
    if any(segment == "" for segment in segments[:-1]):
        raise InvalidPath("Path must not contain empty segments")


def build_upstream_url(base_url: str, prefix: str, strip_prefix: bool, path: str) -> str:
    """Join a validated base URL and request path without string-concatenating raw input.

    Every segment is percent-encoded individually, so the client cannot inject '?', '#',
    '/' or '%' sequences that change meaning on the upstream.
    """
    parts = urlsplit(base_url)
    base_path = parts.path.rstrip("/")
    segments = [] if strip_prefix else [prefix]
    if path:
        segments.extend(path.split("/"))
    encoded = "/".join(quote(segment, safe=_SEGMENT_SAFE) for segment in segments)
    full_path = f"{base_path}/{encoded}" if encoded else (base_path or "/")
    return f"{parts.scheme}://{parts.netloc}{full_path}"
