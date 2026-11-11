"""Honest user-facing messages for every failure state.

WHY SEPARATE MESSAGES?
----------------------
"We're busy", "we're out of quota until tomorrow", "the model service is down",
"the papers don't cover this", and "the papers only partly cover this" are five
different situations. Collapsing them into one message hides the cause and makes
the system look unreliable in a vague way. The brief explicitly asks what a
person sees at 3pm when the daily budget is gone; this module is the answer.

RULES
-----
1. Never invent a reset time. Only mention one when the provider actually sent
   it (``retry-after`` or a documented reset header).
2. Never produce a fabricated answer to avoid a refusal.
3. Tell the user whether their question was processed. A rejected request is
   not the same as an answer that failed.
"""

from __future__ import annotations

TEMPORARY_HIGH_LOAD = (
    "We're experiencing unusually high demand at the moment.\n\n"
    "Your question hasn't been processed yet. Please try again shortly. "
    "We don't want to give you an incomplete or unreliable answer."
)

DAILY_QUOTA_EXHAUSTED = (
    "We've reached today's available processing capacity.\n\n"
    "The research assistant will become available again after the provider's "
    "daily quota resets. Your question hasn't been processed. Please try again "
    "after the reset."
)

PROVIDER_UNAVAILABLE = (
    "The model service is temporarily unavailable, so I can't produce a "
    "grounded answer right now. I have not guessed. Please try again shortly."
)

TIMEOUT = (
    "This question took longer than the time limit, so I stopped rather than "
    "return a half-checked answer. Please try again; a narrower question may "
    "finish faster."
)

QUEUE_FULL = (
    "The assistant is at its safe capacity for waiting requests right now. "
    "Your question was not processed, and no answer was guessed. Please try "
    "again shortly."
)

INTERNAL_ERROR = (
    "The assistant hit an internal error while processing this question. "
    "Your question was not answered, and no answer was guessed. The error has "
    "been recorded for inspection."
)

UNSUPPORTED_QUESTION = (
    "I can only answer questions grounded in three research papers: "
    "*Attention Is All You Need*, *Moshi: a speech-text foundation model for "
    "real-time dialogue*, and *Dynamic Chunking for End-to-End Hierarchical "
    "Sequence Modeling*.\n\n"
    "This question looks outside that library, so I have not sent it to a "
    "model. Rephrasing it as a question about one of those papers will let me "
    "retrieve evidence and cite it."
)


def daily_quota_message(*, reset_hint: str | None = None) -> str:
    """Daily exhaustion message, with a reset time only if one is known."""
    if reset_hint:
        return (
            "We've reached today's available processing capacity.\n\n"
            f"The provider indicates capacity returns in about {reset_hint}. "
            "Your question hasn't been processed. Please try again after that; "
            "we don't want to give you an incomplete or unreliable answer."
        )
    return DAILY_QUOTA_EXHAUSTED


def provider_failure_message(*, kind: str) -> str:
    """Map a provider failure to a human explanation."""
    if kind in {"rate_limit", "rate_limit_rpm", "rate_limit_tpm"}:
        return TEMPORARY_HIGH_LOAD
    if kind.startswith("daily_quota"):
        return DAILY_QUOTA_EXHAUSTED
    if kind == "timeout":
        return TIMEOUT
    return PROVIDER_UNAVAILABLE
