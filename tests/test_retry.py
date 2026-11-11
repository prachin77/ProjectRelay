"""Bounded retry policy: retry the temporary, never the permanent."""

from __future__ import annotations

from the_relay.core.retry import RetryPolicy
from the_relay.errors import (
    PermanentProviderError,
    ProviderError,
    RateLimitError,
    TransientProviderError,
)


def _policy(**overrides) -> RetryPolicy:
    values = {
        "max_retries": 2,
        "base_delay_s": 0.5,
        "max_delay_s": 8.0,
        "jitter": False,
        "minimum_remaining_ms": 750.0,
        "seed": 7,
    }
    values.update(overrides)
    return RetryPolicy(**values)


def test_rate_limit_is_retried_when_budget_allows() -> None:
    decision = _policy().decide(
        RateLimitError("429", retry_after_s=1.0),
        retries_so_far=0,
        remaining_ms=20_000,
    )
    assert decision.should_retry is True
    assert decision.delay_s == 1.0


def test_retry_after_is_honoured_but_capped() -> None:
    decision = _policy(max_delay_s=5.0).decide(
        RateLimitError("429", retry_after_s=99.0),
        retries_so_far=0,
        remaining_ms=60_000,
    )
    assert decision.should_retry is True
    assert decision.delay_s == 5.0


def test_permanent_error_is_never_retried() -> None:
    decision = _policy().decide(
        PermanentProviderError("401", http_status=401),
        retries_so_far=0,
        remaining_ms=20_000,
    )
    assert decision.should_retry is False
    assert "non-retryable" in decision.reason


def test_daily_quota_is_never_retried() -> None:
    decision = _policy().decide(
        ProviderError("daily", kind="daily_quota_tpd", retryable=True),
        retries_so_far=0,
        remaining_ms=20_000,
    )
    assert decision.should_retry is False
    assert "daily quota" in decision.reason


def test_retry_limit_is_enforced() -> None:
    decision = _policy(max_retries=1).decide(
        TransientProviderError("503", http_status=503),
        retries_so_far=1,
        remaining_ms=20_000,
    )
    assert decision.should_retry is False
    assert decision.reason == "retry limit reached"


def test_backoff_that_crosses_the_deadline_is_rejected() -> None:
    decision = _policy(base_delay_s=5.0).decide(
        TransientProviderError("503", http_status=503),
        retries_so_far=0,
        remaining_ms=2_000,
    )
    assert decision.should_retry is False
    assert "deadline" in decision.reason


def test_missing_token_budget_blocks_retry() -> None:
    decision = _policy().decide(
        TransientProviderError("503", http_status=503),
        retries_so_far=0,
        remaining_ms=20_000,
        estimated_tokens_available=False,
    )
    assert decision.should_retry is False
    assert "token budget" in decision.reason
