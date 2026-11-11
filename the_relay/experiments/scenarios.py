"""Load shapes used to test the pipeline.

WHY THESE FIVE
--------------
The brief asks for at least steady, burst, sustained overload, per-minute limit
halved, and daily budget exhausted. Each one tests a different control:

* ``steady``          — offered rate is sustainable; this is the baseline.
* ``burst``           — many requests arrive at once; the bounded queue matters.
* ``overload``        — offered rate stays far above capacity; rejection matters.
* ``reduced_quota``   — the provider's per-minute token budget is halved; the
                        admission policy must choose between delay and refusal.
* ``daily_exhausted`` — the day budget is already gone; no model call is allowed.

Each scenario is an independent run with a fresh quota manager. That is stated
in the report: the runs are not one continuous simulated day. Reusing one day
across five scenarios would make the later rows depend on the earlier ones and
would be much harder to reproduce.

QUESTION SELECTION
------------------
Every scenario uses questions from the same hand-written evaluation set, so a
scenario does not accidentally test a different task. The six questions chosen
here are answerable and cover all three papers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Scenario:
    """One reproducible load shape."""

    name: str
    description: str
    preset: str
    requests: int
    concurrency: int
    interval_ms: int
    deadline_ms: int
    max_concurrency: int
    max_queue_depth: int
    max_queue_wait_ms: int
    quota_overrides: dict[str, Any] = field(default_factory=dict)
    standin_overrides: dict[str, Any] = field(default_factory=dict)
    preconsume_daily: bool = False
    question_ids: tuple[str, ...] = ()

    def settings_overrides(self) -> dict[str, Any]:
        """Settings fields for this scenario (paths are filled by the runner)."""
        overrides: dict[str, Any] = {
            "max_concurrency": self.max_concurrency,
            "max_queue_depth": self.max_queue_depth,
            "max_queue_wait_ms": self.max_queue_wait_ms,
            "request_deadline_ms": self.deadline_ms,
            "retrieval_preset": self.preset,
            "cache_enabled": False,
        }
        overrides.update(self.quota_overrides)
        return overrides


# A small, fixed rotation. Keeping the same questions across scenarios means a
# difference in outcomes is attributable to load, not to a different task.
_SCENARIO_QUESTIONS = ("A01", "A03", "M01", "M03", "D02", "D04")


SCENARIOS: dict[str, Scenario] = {
    "steady": Scenario(
        name="steady",
        description=(
            "Six requests, one at a time, 20 s apart. The offered rate is about "
            "3 requests/minute, which the light retrieval setting should mostly "
            "sustain on a rolling 8k-TPM budget."
        ),
        preset="light",
        requests=6,
        concurrency=1,
        interval_ms=20_000,
        deadline_ms=25_000,
        max_concurrency=1,
        max_queue_depth=2,
        max_queue_wait_ms=10_000,
        question_ids=_SCENARIO_QUESTIONS,
    ),
    "burst": Scenario(
        name="burst",
        description=(
            "Eight requests arrive together after a simulated incident. Three "
            "workers are allowed; the rest wait in a four-deep queue. Token "
            "admission, not request count, is expected to bind."
        ),
        preset="balanced",
        requests=8,
        concurrency=8,
        interval_ms=0,
        deadline_ms=30_000,
        max_concurrency=3,
        max_queue_depth=4,
        max_queue_wait_ms=10_000,
        question_ids=_SCENARIO_QUESTIONS,
    ),
    "overload": Scenario(
        name="overload",
        description=(
            "Twelve requests arrive together and keep arriving faster than they "
            "can be served. Two workers and a two-deep queue force immediate, "
            "honest rejection instead of an unbounded wait."
        ),
        preset="balanced",
        requests=12,
        concurrency=12,
        interval_ms=0,
        deadline_ms=20_000,
        max_concurrency=2,
        max_queue_depth=2,
        max_queue_wait_ms=5_000,
        question_ids=_SCENARIO_QUESTIONS,
    ),
    "reduced_quota": Scenario(
        name="reduced_quota",
        description=(
            "The per-minute token budget is halved to 4,000 TPM. Six requests "
            "are offered at about 4/minute, so the system must delay or refuse "
            "some of them rather than exceed the budget."
        ),
        preset="light",
        requests=6,
        concurrency=1,
        interval_ms=15_000,
        deadline_ms=30_000,
        max_concurrency=1,
        max_queue_depth=2,
        max_queue_wait_ms=10_000,
        quota_overrides={"quota_tpm": 4_000},
        standin_overrides={"tpm": 4_000},
        question_ids=_SCENARIO_QUESTIONS,
    ),
    "daily_exhausted": Scenario(
        name="daily_exhausted",
        description=(
            "The daily token budget has already been consumed before the first "
            "request. Every request must be refused without a model call, with "
            "the daily message shown to the user."
        ),
        preset="balanced",
        requests=4,
        concurrency=4,
        interval_ms=0,
        deadline_ms=10_000,
        max_concurrency=2,
        max_queue_depth=2,
        max_queue_wait_ms=2_000,
        preconsume_daily=True,
        question_ids=_SCENARIO_QUESTIONS,
    ),
}


def get_scenario(name: str) -> Scenario:
    if name not in SCENARIOS:
        raise KeyError(
            f"unknown scenario {name!r}; available: {sorted(SCENARIOS)}"
        )
    return SCENARIOS[name]


def scenario_summary() -> list[dict[str, Any]]:
    """Table-friendly description, used in the report and journal."""
    return [
        {
            "name": scenario.name,
            "preset": scenario.preset,
            "requests": scenario.requests,
            "concurrency": scenario.concurrency,
            "offered_rate_per_min": (
                None
                if scenario.interval_ms <= 0
                else round(60_000 / scenario.interval_ms, 1)
            ),
            "description": scenario.description,
        }
        for scenario in SCENARIOS.values()
    ]
