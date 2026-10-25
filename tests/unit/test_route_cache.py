import pytest

from app.services import routing as routing_module
from app.services.routing import RouteCache, RouteSnapshot


def snap(prefix: str = "echo") -> RouteSnapshot:
    import uuid

    return RouteSnapshot(
        route_id=uuid.uuid4(),
        path_prefix=prefix,
        allowed_methods=("GET",),
        strip_prefix=True,
        base_url="http://echo:9000",
        connect_timeout_seconds=3.0,
        read_timeout_seconds=30.0,
        active=True,
    )


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(routing_module.time, "monotonic", lambda: now["t"])
    return now


def test_miss_then_hit(clock):
    cache, route = RouteCache(), snap()
    assert cache.get("echo") == (False, None)
    cache.put("echo", route, ttl=5)
    assert cache.get("echo") == (True, route)


def test_entries_expire(clock):
    cache = RouteCache()
    cache.put("echo", snap(), ttl=5)
    clock["t"] += 4.9
    assert cache.get("echo")[0] is True
    clock["t"] += 0.2
    assert cache.get("echo")[0] is False


def test_unknown_prefixes_are_cached_as_not_found(clock):
    cache = RouteCache()
    cache.put("nope", None, ttl=5)
    assert cache.get("nope") == (True, None)  # a hit whose answer is "no such route"


def test_clear_empties_everything(clock):
    cache = RouteCache()
    cache.put("a", snap("a"), ttl=5)
    cache.put("b", None, ttl=5)
    cache.clear()
    assert cache.get("a")[0] is False and cache.get("b")[0] is False


def test_cache_is_bounded_so_probing_cannot_grow_memory(clock):
    cache = RouteCache()
    cache.MAX_ENTRIES = 5
    for i in range(50):
        cache.put(f"probe{i}", None, ttl=5)
    assert len(cache._entries) <= 5


def test_snapshots_are_immutable():
    route = snap()
    with pytest.raises(AttributeError):
        route.base_url = "http://elsewhere"  # type: ignore[misc]
