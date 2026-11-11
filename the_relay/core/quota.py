"""Central quota accounting and admission control.

WHY THIS IS CENTRAL
-------------------
Every model call — generation, regeneration, optional verification, every retry
— must pass through one accountant. If different code paths count differently,
the system will exceed the provider limit while believing it is safe.

THE KEY INSIGHT
---------------
The documented free-tier limits are **30 requests/minute** and **8,000
tokens/minute**. For a realistic prompt (~1,500-2,500 tokens), tokens bind long
before requests. A request-count-only gate would admit two 4,000-token calls and
then hit a 429. This manager estimates tokens *before* the call and reconciles
the provider's actual usage after it.

WHAT IS TRACKED
---------------
* requests in the last rolling minute and last rolling day;
* input+output tokens in the last rolling minute and day;
* in-flight calls (application-side, not a documented provider limit);
* reserved tokens for calls that were admitted but have not returned yet.

WHY A ROLLING WINDOW?
---------------------
The provider resets on its own schedule and documents reset headers. Our local
accounting uses a rolling 60-second / 24-hour window because it is conservative
and monotonic-clock based. The probe records the provider's real reset values;
the report distinguishes the two. We never claim the local window *is* the
provider's schedule.

SAFETY RESERVE
--------------
A configurable fraction (default 15%) of every dimension is kept out of reach.
That reserve absorbs estimation error, retries, and the final evaluation runs.
It is a deliberate reduction in throughput in exchange for not exhausting the
day on a burst.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from the_relay.config import Settings
from the_relay.errors import QuotaExceededError
from the_relay.types import QuotaSnapshot


@dataclass(frozen=True)
class QuotaLimits:
    rpm: int
    tpm: int
    rpd: int
    tpd: int
    reserve_fraction: float = 0.15

    def effective(self, value: int) -> int:
        """Apply the safety reserve to one dimension."""
        return max(1, int(value * (1.0 - self.reserve_fraction)))


@dataclass
class QuotaTicket:
    """A reservation for one model call.

    ``token_event`` is a mutable ``[timestamp, tokens]`` pair. When the call
    finishes, ``release`` replaces the estimate with the actual usage. This is
    how estimated admission becomes measured accounting.
    """

    token_event: list[float]
    request_time: float
    estimated_tokens: int
    acquired_at: float


class QuotaManager:
    """Token- and request-aware admission control."""

    def __init__(self, limits: QuotaLimits) -> None:
        self.limits = limits
        self._lock = asyncio.Lock()
        self._request_times: deque[float] = deque()
        self._token_events: deque[list[float]] = deque()
        self._in_flight = 0
        self._peak_in_flight = 0
        self._reserved_tokens = 0

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        reserve_fraction: float | None = None,
    ) -> "QuotaManager":
        return cls(
            QuotaLimits(
                rpm=settings.quota_rpm,
                tpm=settings.quota_tpm,
                rpd=settings.quota_rpd,
                tpd=settings.quota_tpd,
                reserve_fraction=(
                    settings.quota_safety_reserve
                    if reserve_fraction is None
                    else reserve_fraction
                ),
            )
        )

    # ------------------------------------------------------------------
    # Window maths
    # ------------------------------------------------------------------
    def _prune(self, now: float) -> None:
        day_start = now - 86_400.0
        while self._request_times and self._request_times[0] < day_start:
            self._request_times.popleft()
        while self._token_events and self._token_events[0][0] < day_start:
            self._token_events.popleft()

    def _minute_requests(self, now: float) -> int:
        return sum(1 for stamp in self._request_times if stamp >= now - 60.0)

    def _minute_tokens(self, now: float) -> int:
        return int(sum(tokens for stamp, tokens in self._token_events if stamp >= now - 60.0))

    def _day_tokens(self) -> int:
        return int(sum(tokens for _stamp, tokens in self._token_events))

    def _minute_reset_s(self, now: float) -> float:
        recent = [stamp for stamp in self._request_times if stamp >= now - 60.0]
        if not recent:
            return 0.0
        return max(0.0, 60.0 - (now - min(recent)))

    def _day_reset_s(self, now: float) -> float:
        if not self._request_times:
            return 0.0
        return max(0.0, 86_400.0 - (now - min(self._request_times)))

    # ------------------------------------------------------------------
    # Admission
    # ------------------------------------------------------------------
    async def try_acquire(self, estimated_tokens: int) -> tuple[QuotaTicket | None, float, str]:
        """Try to reserve capacity.

        Returns ``(ticket, wait_seconds, reason)``. If ``ticket`` is not None,
        capacity is reserved and the caller must eventually call ``release`` or
        ``cancel``. If ``ticket`` is None, ``wait_seconds`` is the earliest time
        the caller might succeed, and ``reason`` names the binding dimension.
        """
        async with self._lock:
            now = time.monotonic()
            self._prune(now)
            rpm_limit = self.limits.effective(self.limits.rpm)
            tpm_limit = self.limits.effective(self.limits.tpm)
            rpd_limit = self.limits.effective(self.limits.rpd)
            tpd_limit = self.limits.effective(self.limits.tpd)
            minute_requests = self._minute_requests(now)
            minute_tokens = self._minute_tokens(now)

            # Requests per day binds first if the account has been busy all day.
            if len(self._request_times) + 1 > rpd_limit:
                return None, self._day_reset_s(now), "daily request limit"
            # Tokens per day.
            if self._day_tokens() + self._reserved_tokens + estimated_tokens > tpd_limit:
                return None, self._day_reset_s(now), "daily token limit"
            # Requests per minute.
            if minute_requests + 1 > rpm_limit:
                return None, self._minute_reset_s(now), "per-minute request limit"
            # Tokens per minute. Reserved tokens from admitted-but-unfinished
            # calls are included so concurrent admissions cannot oversubscribe.
            if minute_tokens + self._reserved_tokens + estimated_tokens > tpm_limit:
                return None, self._minute_reset_s(now), "per-minute token limit"

            # Admit and reserve.
            event: list[float] = [now, float(estimated_tokens)]
            self._request_times.append(now)
            self._token_events.append(event)
            self._reserved_tokens += estimated_tokens
            self._in_flight += 1
            self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
            return (
                QuotaTicket(
                    token_event=event,
                    request_time=now,
                    estimated_tokens=estimated_tokens,
                    acquired_at=now,
                ),
                0.0,
                "admitted",
            )

    async def acquire(
        self,
        estimated_tokens: int,
        *,
        deadline_monotonic: float,
    ) -> QuotaTicket:
        """Wait for capacity if it is likely to arrive before the deadline.

        This is where a request can be *delayed* rather than dropped. If the
        wait would exceed the user's deadline, the request is rejected
        honestly — the alternative is a user waiting past the point where they
        give up.
        """
        while True:
            ticket, wait_s, reason = await self.try_acquire(estimated_tokens)
            if ticket is not None:
                return ticket
            remaining_s = deadline_monotonic - time.monotonic()
            if remaining_s <= 0:
                raise QuotaExceededError(
                    f"deadline expired while waiting for {reason}",
                    retry_after_s=wait_s,
                )
            if wait_s <= 0 or wait_s > remaining_s:
                raise QuotaExceededError(
                    f"{reason}; next capacity in {wait_s:.1f}s exceeds the "
                    f"remaining deadline of {remaining_s:.1f}s",
                    retry_after_s=wait_s,
                )
            # Wait a little less than the full reset so we re-check promptly.
            await asyncio.sleep(min(wait_s, remaining_s, 5.0))

    async def release(self, ticket: QuotaTicket, actual_tokens: int) -> None:
        """Reconcile a reservation with measured usage."""
        async with self._lock:
            estimated = ticket.estimated_tokens
            # Replace the estimate with the provider's actual count. If the
            # provider used more than estimated, the minute/day windows become
            # slightly more conservative for the next caller, which is safe.
            ticket.token_event[1] = float(max(0, actual_tokens))
            self._reserved_tokens = max(0, self._reserved_tokens - estimated)
            self._in_flight = max(0, self._in_flight - 1)

    async def cancel(self, ticket: QuotaTicket) -> None:
        """Release a reservation for a call that was never sent.

        We remove the request and token events so a pre-flight failure does not
        consume quota that the provider never saw.
        """
        async with self._lock:
            if ticket.request_time in self._request_times:
                self._request_times.remove(ticket.request_time)
            try:
                self._token_events.remove(ticket.token_event)
            except ValueError:
                pass
            self._reserved_tokens = max(0, self._reserved_tokens - ticket.estimated_tokens)
            self._in_flight = max(0, self._in_flight - 1)

    # ------------------------------------------------------------------
    # Test controls and introspection
    # ------------------------------------------------------------------
    async def preconsume(
        self,
        *,
        tokens: int = 0,
        requests: int = 0,
    ) -> None:
        """Consume simulated quota without a model call.

        Used by the "daily budget exhausted" scenario. It is an explicit test
        control, not part of normal operation.
        """
        async with self._lock:
            now = time.monotonic()
            if tokens:
                self._token_events.append([now, float(tokens)])
            for _ in range(requests):
                self._request_times.append(now)

    def snapshot(self) -> QuotaSnapshot:
        now = time.monotonic()
        return QuotaSnapshot(
            requests_this_minute=self._minute_requests(now),
            requests_today=len(self._request_times),
            tokens_this_minute=self._minute_tokens(now),
            tokens_today=self._day_tokens(),
            in_flight=self._in_flight,
            reserved_tokens=self._reserved_tokens,
            requests_remaining_today=max(
                0, self.limits.effective(self.limits.rpd) - len(self._request_times)
            ),
            tokens_remaining_today=max(
                0, self.limits.effective(self.limits.tpd) - self._day_tokens()
            ),
            minute_reset_in_s=self._minute_reset_s(now),
            daily_reset_in_s=self._day_reset_s(now),
        )

    def daily_token_fraction_remaining(self) -> float:
        limit = self.limits.effective(self.limits.tpd)
        used = self._day_tokens()
        return max(0.0, (limit - used) / max(1, limit))

    @property
    def peak_in_flight(self) -> int:
        return self._peak_in_flight

    def to_dict(self) -> dict[str, Any]:
        snap = self.snapshot()
        return {
            "rpm_limit": self.limits.rpm,
            "tpm_limit": self.limits.tpm,
            "rpd_limit": self.limits.rpd,
            "tpd_limit": self.limits.tpd,
            "reserve_fraction": self.limits.reserve_fraction,
            "requests_this_minute": snap.requests_this_minute,
            "requests_today": snap.requests_today,
            "tokens_this_minute": snap.tokens_this_minute,
            "tokens_today": snap.tokens_today,
            "in_flight": snap.in_flight,
            "reserved_tokens": snap.reserved_tokens,
            "requests_remaining_today": snap.requests_remaining_today,
            "tokens_remaining_today": snap.tokens_remaining_today,
        }
