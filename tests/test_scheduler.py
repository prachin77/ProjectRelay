"""Bounded concurrency, bounded queue, and honest deadline rejection."""

from __future__ import annotations

import asyncio
import time

import pytest

from the_relay.core.scheduler import BoundedScheduler
from the_relay.errors import DeadlineExceededError, QueueFullError


async def test_runs_work_and_measures_queue_and_execution() -> None:
    scheduler = BoundedScheduler(
        max_concurrency=2, max_queue_depth=4, max_queue_wait_ms=1_000
    )

    async def work() -> str:
        await asyncio.sleep(0.01)
        return "done"

    result = await scheduler.run(work, deadline_monotonic=time.monotonic() + 2.0)
    assert result.value == "done"
    assert result.queue_wait_ms >= 0
    assert result.execution_ms >= 0


async def test_concurrency_cap_is_not_exceeded() -> None:
    scheduler = BoundedScheduler(
        max_concurrency=1, max_queue_depth=4, max_queue_wait_ms=5_000
    )
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def work() -> None:
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.03)
        async with lock:
            active -= 1

    await asyncio.gather(
        *(
            scheduler.run(work, deadline_monotonic=time.monotonic() + 5.0)
            for _ in range(4)
        )
    )
    assert peak == 1
    assert scheduler.stats()["peak_in_flight"] == 1
    assert scheduler.stats()["submitted"] == 4


async def test_full_queue_is_rejected_immediately() -> None:
    scheduler = BoundedScheduler(
        max_concurrency=1, max_queue_depth=0, max_queue_wait_ms=5_000
    )
    release = asyncio.Event()

    async def blocking() -> None:
        await release.wait()

    first = asyncio.create_task(
        scheduler.run(blocking, deadline_monotonic=time.monotonic() + 5.0)
    )
    await asyncio.sleep(0.01)  # let the first call acquire the worker

    async def quick() -> None:
        return None

    with pytest.raises(QueueFullError):
        await scheduler.run(quick, deadline_monotonic=time.monotonic() + 5.0)

    release.set()
    await first
    assert scheduler.stats()["rejected"] == 1


async def test_expired_deadline_is_rejected_before_queueing() -> None:
    scheduler = BoundedScheduler(
        max_concurrency=1, max_queue_depth=1, max_queue_wait_ms=1_000
    )

    async def work() -> None:
        return None

    with pytest.raises(DeadlineExceededError):
        await scheduler.run(work, deadline_monotonic=time.monotonic() - 1.0)


async def test_execution_that_exceeds_the_deadline_raises() -> None:
    scheduler = BoundedScheduler(
        max_concurrency=1, max_queue_depth=1, max_queue_wait_ms=1_000
    )

    async def slow() -> None:
        await asyncio.sleep(0.2)

    with pytest.raises(DeadlineExceededError):
        await scheduler.run(slow, deadline_monotonic=time.monotonic() + 0.05)
    assert scheduler.stats()["expired"] == 1
