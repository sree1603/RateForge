"""Recording usage events without slowing down or breaking the request path.

`UsageRecorder` is the interface the rest of the code depends on: call `record(event)` and
carry on. `BufferedUsageRecorder` is the current implementation: it puts events on an
in-process queue and a background task writes them to PostgreSQL in batches.

Because callers only see `record()`, the storage can later change (a Redis stream, a message
queue) without touching the gateway.

Honest limits of this implementation:
  * Events still in the queue are lost if the process is killed, and events in a batch that
    fails to write are dropped (and counted). Usage analytics here are best-effort, not an
    audit log or a billing ledger.
  * If the queue is full (the database is much slower than traffic) new events are dropped
    and counted instead of making requests wait.
"""

import asyncio
import logging
from dataclasses import asdict
from typing import Protocol

from sqlalchemy import insert

from app.core.usage_context import UsageRecord
from app.models import UsageEvent

logger = logging.getLogger("rateforge.usage")


class UsageRecorder(Protocol):
    def record(self, event: UsageRecord) -> None: ...


class BufferedUsageRecorder:
    def __init__(
        self,
        session_factory,
        *,
        queue_size: int = 10_000,
        batch_size: int = 100,
        flush_interval_seconds: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._queue: asyncio.Queue[UsageRecord] = asyncio.Queue(maxsize=queue_size)
        self._batch_size = batch_size
        self._flush_interval = flush_interval_seconds
        self._wakeup = asyncio.Event()
        self._flush_lock = asyncio.Lock()
        self._closing = False
        self._task: asyncio.Task | None = None
        self.written = 0  # events stored
        self.dropped = 0  # events discarded because the queue was full
        self.failed = 0  # events lost because a batch could not be written

    # ---- called on the request path: must be instant and must never raise ----
    def record(self, event: UsageRecord) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                logger.warning(
                    "usage queue full; dropping events", extra={"path": str(self.dropped)}
                )
            return
        if self._queue.qsize() >= self._batch_size:
            self._wakeup.set()  # a full batch is ready: do not wait for the timer

    # ---- background writing ----
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="usage-recorder")

    async def _run(self) -> None:
        while not self._closing:
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._flush_interval)
            except TimeoutError:
                pass
            self._wakeup.clear()
            await self.flush()

    async def flush(self) -> None:
        """Write everything currently queued. Safe to call at any time (the tests do)."""
        async with self._flush_lock:
            while not self._queue.empty():
                batch: list[UsageRecord] = []
                while len(batch) < self._batch_size and not self._queue.empty():
                    batch.append(self._queue.get_nowait())
                await self._write(batch)

    async def _write(self, batch: list[UsageRecord]) -> None:
        try:
            async with self._session_factory() as session:
                await session.execute(insert(UsageEvent), [asdict(e) for e in batch])
                await session.commit()
        except Exception as exc:  # never let analytics take the gateway down
            self.failed += len(batch)
            logger.error(
                "could not write usage events",
                extra={"path": f"{type(exc).__name__}:{len(batch)}"},
            )
        else:
            self.written += len(batch)

    async def stop(self) -> None:
        """Flush what is left. Called at shutdown, before the database engine is closed."""
        self._closing = True
        self._wakeup.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
            self._task = None
        await self.flush()
