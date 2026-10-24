import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.usage_context import UsageContext, UsageRecord
from app.repositories.usage import InvalidCursor, decode_cursor, encode_cursor
from app.services.usage import BufferedUsageRecorder


def make_event(n: int = 0, **overrides) -> UsageRecord:
    fields = {
        "tenant_id": uuid.uuid4(),
        "route_id": None,
        "route_prefix": "echo",
        "request_id": f"req_{n}",
        "method": "GET",
        "status_code": 200,
        "outcome": "forwarded",
        "latency_ms": 12.5,
        "occurred_at": datetime.now(UTC),
    }
    return UsageRecord(**{**fields, **overrides})


class FakeSession:
    def __init__(self, sink: list, fail: bool) -> None:
        self.sink, self.fail = sink, fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, rows):
        if self.fail:
            raise RuntimeError("database is down")
        self.sink.append(rows)

    async def commit(self):
        pass


def recorder_with(sink: list, *, fail: bool = False, **kwargs) -> BufferedUsageRecorder:
    return BufferedUsageRecorder(lambda: FakeSession(sink, fail), **kwargs)


async def test_flush_writes_queued_events_in_one_batch():
    batches: list = []
    recorder = recorder_with(batches)
    for i in range(3):
        recorder.record(make_event(i))
    assert batches == []  # nothing is written on the request path
    await recorder.flush()
    assert len(batches) == 1 and len(batches[0]) == 3
    assert recorder.written == 3


async def test_large_backlogs_are_written_in_bounded_batches():
    batches: list = []
    recorder = recorder_with(batches, batch_size=100)
    for i in range(250):
        recorder.record(make_event(i))
    await recorder.flush()
    assert [len(b) for b in batches] == [100, 100, 50]
    assert recorder.written == 250


async def test_a_full_queue_drops_and_counts_instead_of_blocking():
    recorder = recorder_with([], queue_size=2)
    for i in range(5):
        recorder.record(make_event(i))  # must return immediately every time
    assert recorder.dropped == 3
    batches: list = []
    recorder._session_factory = lambda: FakeSession(batches, False)
    await recorder.flush()
    assert recorder.written == 2


async def test_a_failing_database_is_counted_and_never_raises():
    recorder = recorder_with([], fail=True)
    recorder.record(make_event(1))
    recorder.record(make_event(2))
    await recorder.flush()  # must not raise
    assert recorder.failed == 2 and recorder.written == 0


async def test_the_recorder_recovers_after_a_failed_batch():
    sink: list = []
    recorder = recorder_with(sink, fail=True)
    recorder.record(make_event(1))
    await recorder.flush()
    recorder._session_factory = lambda: FakeSession(sink, False)
    recorder.record(make_event(2))
    await recorder.flush()
    assert (recorder.failed, recorder.written) == (1, 1)


async def test_background_task_flushes_on_the_timer():
    sink: list = []
    recorder = recorder_with(sink, flush_interval_seconds=0.05)
    recorder.start()
    recorder.record(make_event(1))
    await asyncio.sleep(0.4)
    assert recorder.written == 1
    await recorder.stop()


async def test_a_full_batch_is_written_without_waiting_for_the_timer():
    sink: list = []
    recorder = recorder_with(sink, batch_size=3, flush_interval_seconds=30)
    recorder.start()
    for i in range(3):
        recorder.record(make_event(i))
    await asyncio.sleep(0.3)
    assert recorder.written == 3  # the 30-second timer never had to fire
    await recorder.stop()


async def test_stop_flushes_whatever_is_left():
    sink: list = []
    recorder = recorder_with(sink, flush_interval_seconds=30)
    recorder.start()
    recorder.record(make_event(1))
    await recorder.stop()
    assert recorder.written == 1


# ---------------- usage context ----------------
def test_only_attributable_requests_are_recordable():
    assert not UsageContext().recordable  # unauthenticated / unknown route
    assert not UsageContext(tenant_id=uuid.uuid4()).recordable  # no route resolved
    assert UsageContext(tenant_id=uuid.uuid4(), route_prefix="echo").recordable
    assert UsageContext().outcome == "rejected"  # the default until the request gets further


# ---------------- cursors ----------------
def test_cursor_round_trip():
    when = datetime(2026, 10, 20, 12, 30, 45, 123456, tzinfo=UTC)
    cursor = encode_cursor(SimpleNamespace(occurred_at=when, id=987654321))
    assert decode_cursor(cursor) == (when, 987654321)
    assert "=" not in cursor and "|" not in cursor  # safe in a URL


@pytest.mark.parametrize(
    "bad",
    ["", "not-a-cursor", "!!!!", "YWJj", "MjAyNi0xMC0yMFQxMjowMDowMHwx"],  # last: naive timestamp
)
def test_bad_cursors_are_rejected(bad):
    with pytest.raises(InvalidCursor):
        decode_cursor(bad)
