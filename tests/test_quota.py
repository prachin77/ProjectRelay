"""Quota accounting and admission control.

The bugs these tests pin down were observed in a real Groq run:

* a deadline in milliseconds was compared with ``time.monotonic()`` seconds,
  producing a 90-million-second wait budget;
* the exact rolling-window boundary returned ``wait_s == 0`` and the caller
  raised instead of re-checking, rejecting four admissible requests.
"""

from __future__ import annotations

import time

import pytest

from the_relay.core.quota import QuotaLimits, QuotaManager
from the_relay.errors import QuotaExceededError


def _manager(**overrides) -> QuotaManager:
    values = {
        "rpm": 30,
        "tpm": 8_000,
        "rpd": 1_000,
        "tpd": 200_000,
        "reserve_fraction": 0.0,
    }
    values.update(overrides)
    return QuotaManager(QuotaLimits(**values))


async def test_admits_within_all_limits() -> None:
    quota = _manager()
    ticket, wait_s, reason = await quota.try_acquire(1_000)
    assert ticket is not None
    assert wait_s == 0.0
    assert reason == "admitted"


async def test_reserve_fraction_reduces_effective_limit() -> None:
    limits = QuotaLimits(rpm=10, tpm=100, rpd=10, tpd=1_000, reserve_fraction=0.5)
    assert limits.effective(100) == 50
    # Never zero, even for a tiny limit.
    assert limits.effective(1) == 1


async def test_token_limit_rejects_and_reports_wait() -> None:
    quota = _manager(tpm=100)
    assert (await quota.try_acquire(60))[0] is not None
    ticket, wait_s, reason = await quota.try_acquire(60)
    assert ticket is None
    assert "token" in reason
    assert wait_s > 0


async def test_release_reconciles_estimate_with_actual_usage() -> None:
    quota = _manager(tpm=100)
    ticket, _, _ = await quota.try_acquire(60)
    assert ticket is not None
    await quota.release(ticket, 10)
    # The estimate (60) was replaced by the actual (10), so 60 more fits.
    assert (await quota.try_acquire(60))[0] is not None


async def test_cancel_frees_a_reservation_that_was_never_sent() -> None:
    quota = _manager(tpm=100)
    ticket, _, _ = await quota.try_acquire(60)
    assert ticket is not None
    await quota.cancel(ticket)
    assert (await quota.try_acquire(60))[0] is not None


async def test_daily_token_limit_is_reported_separately() -> None:
    quota = _manager(tpd=100)
    await quota.preconsume(tokens=100)
    ticket, wait_s, reason = await quota.try_acquire(1)
    assert ticket is None
    assert "daily token" in reason
    assert wait_s > 0


async def test_deadline_expiry_raises_instead_of_waiting_forever() -> None:
    quota = _manager(tpm=100)
    await quota.try_acquire(100)
    with pytest.raises(QuotaExceededError):
        await quota.acquire(50, deadline_monotonic=time.monotonic() + 0.05)


async def test_zero_wait_boundary_is_retried_not_rejected() -> None:
    quota = _manager(tpm=100)
    # Simulate the exact boundary: the event is still inside the inclusive
    # 60-second window but is about to fall out. The old code raised here.
    now = time.monotonic()
    quota._token_events.append([now - 60.0, 100.0])
    ticket = await quota.acquire(10, deadline_monotonic=time.monotonic() + 5.0)
    assert ticket is not None
    await quota.release(ticket, 10)


async def test_snapshot_reports_remaining_daily_budget() -> None:
    quota = _manager(tpd=1_000)
    await quota.preconsume(tokens=300, requests=2)
    snapshot = quota.snapshot()
    assert snapshot.requests_today == 2
    assert snapshot.tokens_today == 300
    assert snapshot.tokens_remaining_today == 700
