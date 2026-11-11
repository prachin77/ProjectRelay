"""Bounded retry policy.

WHAT GETS RETRIED
-----------------
Only failures that are plausibly temporary:

* HTTP 429 (rate limit) — but only if the deadline and token budget can absorb
  the retry;
* HTTP 5xx (server error), connection reset, read timeout.

WHAT NEVER GETS RETRIED
-----------------------
* 400, 401, 403, 404, 413, 422 — the request is wrong, the key is wrong, or the
  resource does not exist. Retrying cannot help and may look abusive.
* Daily quota exhaustion — waiting seconds cannot restore a daily budget.
* A retry whose backoff would cross the user's deadline.

WHY BOUNDED EXPONENTIAL BACKOFF WITH JITTER?
--------------------------------------------
Bounded: a retry storm is worse than a failure. Exponential: give a struggling
service room to recover. Jitter: if many clients retry in lock-step, they
recreate the burst that caused the failure.

THE HARMFUL RETRY WE WANT TO DEMONSTRATE
----------------------------------------
Retrying a 429 *immediately* consumes another request against RPM while the
token bucket is still empty. It can convert a short delay into two failures and
a longer wait. The policy avoids this by honouring ``retry-after`` and by
checking the local token budget before retrying. The experiment traces are
searched for a case where a retry increased total latency without improving the
outcome; if found, it is reported honestly.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from the_relay.errors import ProviderError


@dataclass(frozen=True)
class RetryDecision:
    """Whether to retry, how long to wait, and why."""

    should_retry: bool
    delay_s: float
    reason: str


class RetryPolicy:
    """Classified, bounded, jittered retries."""

    def __init__(
        self,
        *,
        max_retries: int = 1,
        base_delay_s: float = 0.5,
        max_delay_s: float = 8.0,
        jitter: bool = True,
        minimum_remaining_ms: float = 750.0,
        seed: int = 42,
    ) -> None:
        self.max_retries = max(0, max_retries)
        self.base_delay_s = max(0.0, base_delay_s)
        self.max_delay_s = max(self.base_delay_s, max_delay_s)
        self.jitter = jitter
        self.minimum_remaining_ms = minimum_remaining_ms
        self._rng = random.Random(seed)

    def decide(
        self,
        error: ProviderError,
        *,
        retries_so_far: int,
        remaining_ms: float,
        estimated_tokens_available: bool = True,
    ) -> RetryDecision:
        """Decide whether to retry one failed model call.

        Parameters
        ----------
        error:
            The typed provider error.
        retries_so_far:
            Number of retries already attempted for this *call* (not for the
            whole user request).
        remaining_ms:
            Time left before the user's deadline.
        estimated_tokens_available:
            The quota manager's view of whether another call is affordable.
        """
        if retries_so_far >= self.max_retries:
            return RetryDecision(False, 0.0, "retry limit reached")
        if not error.retryable:
            return RetryDecision(False, 0.0, f"non-retryable error: {error.kind}")
        if error.kind.startswith("daily_quota"):
            return RetryDecision(
                False, 0.0, "daily quota is exhausted; waiting will not restore it"
            )
        if not estimated_tokens_available:
            return RetryDecision(
                False, 0.0, "token budget cannot absorb another attempt"
            )

        # Exponential backoff: base, 2*base, 4*base, ... capped.
        backoff = min(self.max_delay_s, self.base_delay_s * (2 ** retries_so_far))
        if error.retry_after_s is not None:
            # The provider told us when it expects capacity. Honour it, but do
            # not let a pathological value exceed our own cap.
            backoff = min(self.max_delay_s, max(backoff, error.retry_after_s))
        if self.jitter and backoff > 0:
            # "Equal jitter": half fixed, half random. This avoids a thundering
            # herd without making the wait unpredictable enough to hurt tests.
            backoff = backoff / 2.0 + self._rng.uniform(0.0, backoff / 2.0)

        if remaining_ms < backoff * 1000.0 + self.minimum_remaining_ms:
            return RetryDecision(
                False,
                backoff,
                "backoff would cross the request deadline",
            )

        return RetryDecision(
            True,
            backoff,
            f"retryable {error.kind} error; backoff {backoff:.2f}s",
        )
