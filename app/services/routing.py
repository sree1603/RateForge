import logging
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
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


@dataclass(frozen=True)
class RouteSnapshot:
    """Everything the gateway needs to serve a request for one route, as plain values.

    A snapshot, not an ORM object, so it can be cached and shared between requests safely
    (an ORM object belongs to one database session).
    """

    route_id: uuid.UUID
    path_prefix: str
    allowed_methods: tuple[str, ...]
    strip_prefix: bool
    base_url: str
    connect_timeout_seconds: float
    read_timeout_seconds: float
    active: bool  # the route and its upstream are both enabled


class RouteCache:
    """Per-process cache of prefix -> RouteSnapshot, so requests do not query the database
    for something that almost never changes.

    - Changes made through this process (create route, enable/disable) clear the cache at once.
    - Changes made elsewhere (another instance, a database edit) are noticed within
      `route_cache_ttl_seconds`. That delay is the price of the speedup, and is how long a
      disabled route can keep serving on the other instances.
    - Unknown prefixes are cached as "not found" so probing cannot hammer the database; the
      cache is bounded, and simply emptied if it ever fills.
    """

    MAX_ENTRIES = 1000

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, RouteSnapshot | None]] = {}

    def get(self, prefix: str) -> tuple[bool, RouteSnapshot | None]:
        entry = self._entries.get(prefix)
        if entry is None or entry[0] <= time.monotonic():
            self._entries.pop(prefix, None)
            return False, None
        return True, entry[1]

    def put(self, prefix: str, snapshot: RouteSnapshot | None, ttl: float) -> None:
        if len(self._entries) >= self.MAX_ENTRIES:
            self._entries.clear()
        self._entries[prefix] = (time.monotonic() + ttl, snapshot)

    def clear(self) -> None:
        self._entries.clear()


route_cache = RouteCache()


async def get_route_snapshot(session: AsyncSession, prefix: str) -> RouteSnapshot | None:
    ttl = get_settings().route_cache_ttl_seconds
    if ttl > 0:
        hit, cached = route_cache.get(prefix)
        if hit:
            return cached
    route = await routing_repo.get_route_by_prefix(session, prefix)
    snapshot = (
        RouteSnapshot(
            route_id=route.id,
            path_prefix=route.path_prefix,
            allowed_methods=tuple(route.allowed_methods),
            strip_prefix=route.strip_prefix,
            base_url=route.upstream.base_url,
            connect_timeout_seconds=route.upstream.connect_timeout_seconds,
            read_timeout_seconds=route.upstream.read_timeout_seconds,
            active=route.enabled and route.upstream.enabled,
        )
        if route is not None
        else None
    )
    if ttl > 0:
        route_cache.put(prefix, snapshot, ttl)
    return snapshot


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
    route_cache.clear()
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
    route_cache.clear()  # a prefix cached as "not found" must start working immediately
    return route


async def set_upstream_enabled(
    session: AsyncSession, upstream_id: uuid.UUID, enabled: bool
) -> Upstream:
    upstream = await routing_repo.get_upstream(session, upstream_id)
    if upstream is None:
        raise NotFoundError("Upstream not found")
    upstream.enabled = enabled
    await session.commit()
    route_cache.clear()
    return upstream


async def set_route_enabled(session: AsyncSession, route_id: uuid.UUID, enabled: bool) -> Route:
    route = await routing_repo.get_route(session, route_id)
    if route is None:
        raise NotFoundError("Route not found")
    route.enabled = enabled
    await session.commit()
    route_cache.clear()
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
