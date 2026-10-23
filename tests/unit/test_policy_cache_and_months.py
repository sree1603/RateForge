import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.services import policy as policy_module
from app.services.policy import PolicyCache, ResolvedPolicy
from app.services.rate_limiter import _month_info

PLAN = ResolvedPolicy("Basic", 60, 60, None)


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(policy_module.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(get_settings(), "policy_cache_ttl_seconds", 30.0)
    return now


def test_cache_miss_then_hit(clock):
    cache, tenant = PolicyCache(), uuid.uuid4()
    assert cache.get(tenant) == (False, None)
    cache.put(tenant, PLAN)
    assert cache.get(tenant) == (True, PLAN)


def test_entries_expire_after_the_ttl(clock):
    cache, tenant = PolicyCache(), uuid.uuid4()
    cache.put(tenant, PLAN)
    clock["t"] += 29.9
    assert cache.get(tenant)[0] is True
    clock["t"] += 0.2
    assert cache.get(tenant)[0] is False


def test_absence_of_a_plan_is_cached_too(clock):
    cache, tenant = PolicyCache(), uuid.uuid4()
    cache.put(tenant, None)
    assert cache.get(tenant) == (True, None)


def test_invalidate_and_clear(clock):
    cache, a, b = PolicyCache(), uuid.uuid4(), uuid.uuid4()
    cache.put(a, PLAN)
    cache.put(b, PLAN)
    cache.invalidate(a)
    assert cache.get(a)[0] is False and cache.get(b)[0] is True
    cache.clear()
    assert cache.get(b)[0] is False


def test_cache_is_bounded(clock):
    cache = PolicyCache()
    cache.MAX_ENTRIES = 3
    for _ in range(10):
        cache.put(uuid.uuid4(), PLAN)
    assert len(cache._entries) <= 3


def test_month_info_label_and_seconds_to_month_end():
    label, seconds = _month_info(datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC))
    assert label == "202610"
    assert seconds == 11 * 86400 + 12 * 3600  # 20 Oct 12:00 -> 1 Nov 00:00


def test_month_info_december_rolls_into_next_year():
    label, seconds = _month_info(datetime(2026, 12, 31, 23, 59, 0, tzinfo=UTC))
    assert label == "202612"
    assert seconds == 60


def test_month_info_first_instant_of_a_month():
    label, seconds = _month_info(datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC))
    assert label == "202602"
    assert seconds == 28 * 86400  # 2026 is not a leap year
