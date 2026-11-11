"""Exception types shared across The Relay.

WHY TYPED ERRORS?
-----------------
A retry policy must distinguish "the network hiccuped" from "your API key is
wrong". If every failure is a generic ``Exception``, the only safe policy is
"never retry" (slow) or "always retry" (wasteful and sometimes harmful). The
classes here make the distinction explicit and testable.

The hierarchy is intentionally small:

    RelayError
      +-- ConfigurationError        (our fault; do not retry)
      +-- ProviderError             (the model service)
      |     +-- RateLimitError      (429; retry only if deadline/budget allow)
      |     +-- TransientProviderError (5xx, network; bounded retry)
      |     +-- PermanentProviderError (4xx; never retry)
      |     +-- ProviderTimeoutError   (read timeout; bounded retry)
      +-- QuotaExceededError        (our quota manager; reject or wait)
      +-- QueueFullError            (bounded queue; reject immediately)
      +-- DeadlineExceededError     (user deadline; give up honestly)

None of these constructors accept an API key or a full request body, so a
trace can log the error without leaking a credential.
"""

from __future__ import annotations


class RelayError(Exception):
    """Base class for every error The Relay raises deliberately."""


class ConfigurationError(RelayError):
    """A required setting is missing or invalid (e.g. no Groq key)."""


class ProviderError(RelayError):
    """The model provider failed a call.

    Attributes
    ----------
    kind:
        Short machine-readable category, e.g. ``rate_limit``, ``timeout``,
        ``server_error``, ``auth_error``, ``connection_error``.
    retryable:
        The provider's failure is plausibly temporary. This is only a *hint*;
        the retry policy also considers the deadline and the token budget.
    retry_after_s:
        The provider's requested wait, if it supplied one (``retry-after``).
    http_status:
        HTTP status when the failure came from an HTTP response.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "provider_error",
        retryable: bool = False,
        retry_after_s: float | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.http_status = http_status


class RateLimitError(ProviderError):
    """HTTP 429 or an equivalent provider refusal."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_s: float | None = None,
        http_status: int = 429,
        kind: str = "rate_limit",
    ) -> None:
        super().__init__(
            message,
            kind=kind,
            retryable=True,
            retry_after_s=retry_after_s,
            http_status=http_status,
        )


class TransientProviderError(ProviderError):
    """5xx, connection reset, or another temporary provider failure."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(
            message,
            kind="transient",
            retryable=True,
            http_status=http_status,
        )


class PermanentProviderError(ProviderError):
    """4xx other than 429: retrying cannot help."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(
            message,
            kind="permanent",
            retryable=False,
            http_status=http_status,
        )


class ProviderTimeoutError(ProviderError):
    """The provider did not answer inside the allowed time."""

    def __init__(self, message: str = "provider call timed out") -> None:
        super().__init__(message, kind="timeout", retryable=True)


class QuotaExceededError(RelayError):
    """Our own quota manager refused the call.

    ``retry_after_s`` is the time until capacity is expected, when known. A
    caller may wait if the user deadline allows; otherwise it must reject.
    """

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class QueueFullError(RelayError):
    """The bounded wait queue is full. Reject immediately, do not accumulate."""


class DeadlineExceededError(RelayError):
    """The per-request deadline expired before an acceptable answer existed."""
