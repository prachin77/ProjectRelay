"""Bounded concurrency and a bounded wait queue.

THE FAILURE MODE THIS PREVENTS
------------------------------
Unbounded queues look harmless: "we'll process it eventually." In practice they
turn overload into a different failure — users wait past the point where they
give up, memory grows, and every request eventually times out. A bounded queue
plus an honest rejection is more useful than a slow lie.

THE THREE CONTROLS
------------------
1. **Concurrency cap** (``max_concurrency``): how many requests may execute at
   once. This is an application policy, not a documented provider limit.
2. **Queue capacity** (``max_queue_depth``): how many requests may wait. When
   full, new requests are rejected immediately.
3. **Deadline** (``deadline_monotonic``): a request that cannot start or finish
   before its deadline is expired, not processed late.

FAIRNESS
--------
``asyncio.Semaphore`` wakes waiters in roughly FIFO order. That is good enough
for this assignment; a strict FIFO ticket system would add code without a
measurable benefit at this scale. The report says "approximately FIFO" rather
than claiming a guarantee.

MEASUREMENT
-----------
``queue_wait_ms`` is recorded per request, and ``peak_queue_length`` is kept for
Table 2b. A queue that never fills and a queue that always fills produce very
different user experiences even at the same average latency.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, TypeVar

from the_relay.errors import DeadlineExceededError, QueueFullError

T = TypeVar("T")


@dataclass(frozen=True)
class ScheduledResult(Generic[T]):
    """The value returned by a scheduled call plus its queue measurement."""

    value: T
    queue_wait_ms: float
    execution_ms: float


class BoundedScheduler:
    """Run async work with a concurrency cap, a bounded queue, and deadlines."""

    def __init__(
        self,
        *,
        max_concurrency: int,
        max_queue_depth: int,
        max_queue_wait_ms: int,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if max_queue_depth < 0:
            raise ValueError("max_queue_depth must be >= 0")
        self.max_concurrency = max_concurrency
        self.max_queue_depth = max_queue_depth
        self.max_queue_wait_ms = max_queue_wait_ms
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._waiting = 0
        self._in_flight = 0
        self._peak_queue = 0
        self._peak_in_flight = 0
        self._submitted = 0
        self._rejected = 0
        self._expired = 0

    async def run(
        self,
        work: Callable[[], Awaitable[T]],
        *,
        deadline_monotonic: float,
    ) -> ScheduledResult[T]:
        """Queue and execute ``work``.

        Raises:
        - ``QueueFullError`` if the wait queue is already at capacity;
        - ``DeadlineExceededError`` if the queue wait or execution would exceed
          the request deadline.
        """
        self._submitted += 1
        now = time.monotonic()
        remaining_s = deadline_monotonic - now
        if remaining_s <= 0:
            self._expired += 1
            raise DeadlineExceededError("request deadline expired before queueing")

        # No-queue mode: if the semaphore is held, reject immediately.
        if self.max_queue_depth == 0 and self._semaphore.locked():
            self._rejected += 1
            raise QueueFullError("queue is disabled and all workers are busy")

        if self.max_queue_depth > 0 and self._waiting >= self.max_queue_depth:
            self._rejected += 1
            raise QueueFullError(
                f"queue is full ({self.max_queue_depth} waiting); rejecting "
                "rather than accumulating unbounded work"
            )

        queue_started = time.monotonic()
        self._waiting += 1
        self._peak_queue = max(self._peak_queue, self._waiting)
        acquired = False
        try:
            wait_budget_s = min(
                self.max_queue_wait_ms / 1000.0,
                max(0.0, deadline_monotonic - time.monotonic()),
            )
            if wait_budget_s <= 0:
                self._expired += 1
                raise DeadlineExceededError("no time left to wait for a worker")
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=wait_budget_s)
                acquired = True
            except asyncio.TimeoutError as exc:
                self._expired += 1
                raise DeadlineExceededError(
                    f"waited {wait_budget_s * 1000:.0f} ms for a worker and gave up"
                ) from exc

            queue_wait_ms = (time.monotonic() - queue_started) * 1000.0
            self._waiting = max(0, self._waiting - 1)
            self._in_flight += 1
            self._peak_in_flight = max(self._peak_in_flight, self._in_flight)

            execution_started = time.monotonic()
            remaining_s = deadline_monotonic - execution_started
            if remaining_s <= 0:
                self._expired += 1
                raise DeadlineExceededError("deadline expired before execution")
            try:
                value = await asyncio.wait_for(work(), timeout=remaining_s)
            except asyncio.TimeoutError as exc:
                self._expired += 1
                raise DeadlineExceededError(
                    "request deadline expired during execution"
                ) from exc
            execution_ms = (time.monotonic() - execution_started) * 1000.0
            return ScheduledResult(
                value=value,
                queue_wait_ms=queue_wait_ms,
                execution_ms=execution_ms,
            )
        finally:
            if acquired:
                self._in_flight = max(0, self._in_flight - 1)
                self._semaphore.release()
            elif self._waiting > 0:
                # We were counted as waiting but never acquired (timeout or
                # rejection); decrement so the queue length is accurate.
                self._waiting -= 1

    def stats(self) -> dict[str, int]:
        """Counters for the experiment report."""
        return {
            "submitted": self._submitted,
            "peak_queue_length": self._peak_queue,
            "peak_in_flight": self._peak_in_flight,
            "rejected": self._rejected,
            "expired": self._expired,
            "waiting_now": self._waiting,
            "in_flight_now": self._in_flight,
        }
