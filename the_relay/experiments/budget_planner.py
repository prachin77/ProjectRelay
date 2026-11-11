"""Plan an experiment against the documented Groq free-tier budget.

WHY THIS EXISTS
---------------
The most expensive mistake in this assignment is discovering halfway through an
evaluation that the day's token budget cannot cover it. The binding limit is
tokens per minute (8,000), not requests per minute (30), because a grounded
prompt is roughly 2,000 tokens. This module computes the sustainable request
rate and the day capacity *before* a run, using the same token estimator the
pipeline uses for admission.

WHAT IS AN ESTIMATE
-------------------
``estimated_input_tokens`` is the preset's context cap plus the fixed prompt
overhead. Real input is often smaller because retrieval returns less text than
the cap. The estimate is deliberately conservative, matching the admission
policy's bias.

The numbers here are planning aids, not measurements. The report prints measured
tokens from ``results.csv``; if the plan and the measurement disagree, the
measurement wins and the difference is discussed.
"""

from __future__ import annotations

from dataclasses import dataclass

from the_relay.config import RetrievalPreset, Settings
from the_relay.eval.metrics import (
    PRICE_INPUT_PER_MILLION,
    PRICE_OUTPUT_PER_MILLION,
)

# System prompt, instructions, chat-template overhead and a small safety margin.
PROMPT_OVERHEAD_TOKENS = 350


@dataclass(frozen=True)
class BudgetPlan:
    preset: str
    estimated_input_tokens: int
    max_output_tokens: int
    estimated_total_tokens: int
    effective_tpm: int
    effective_tpd: int
    requests_per_minute: float
    requests_per_day: int
    seconds_per_request_at_tpm: float
    seconds_for_100_requests: float
    shadow_cost_per_1000_answers_usd: float

    def to_dict(self) -> dict[str, object]:
        return {
            "preset": self.preset,
            "estimated_input_tokens": self.estimated_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "estimated_total_tokens": self.estimated_total_tokens,
            "effective_tpm": self.effective_tpm,
            "effective_tpd": self.effective_tpd,
            "requests_per_minute": round(self.requests_per_minute, 2),
            "requests_per_day": self.requests_per_day,
            "seconds_per_request_at_tpm": round(self.seconds_per_request_at_tpm, 2),
            "seconds_for_100_requests": round(self.seconds_for_100_requests, 1),
            "shadow_cost_per_1000_answers_usd": round(
                self.shadow_cost_per_1000_answers_usd, 4
            ),
        }


def plan_for_preset(
    settings: Settings,
    preset: RetrievalPreset,
    *,
    max_output_tokens: int | None = None,
) -> BudgetPlan:
    """Estimate the sustainable rate for one retrieval preset."""
    output = (
        settings.max_output_tokens
        if max_output_tokens is None
        else max_output_tokens
    )
    estimated_input = preset.max_context_tokens + PROMPT_OVERHEAD_TOKENS
    estimated_total = estimated_input + output

    reserve = settings.quota_safety_reserve
    effective_tpm = max(1, int(settings.quota_tpm * (1.0 - reserve)))
    effective_tpd = max(1, int(settings.quota_tpd * (1.0 - reserve)))

    requests_per_minute = effective_tpm / max(1, estimated_total)
    requests_per_day = effective_tpd // max(1, estimated_total)
    seconds_per_request = 60.0 / max(0.001, requests_per_minute)

    shadow_cost = (
        estimated_input / 1_000_000.0 * PRICE_INPUT_PER_MILLION
        + output / 1_000_000.0 * PRICE_OUTPUT_PER_MILLION
    ) * 1000.0

    return BudgetPlan(
        preset=preset.name,
        estimated_input_tokens=estimated_input,
        max_output_tokens=output,
        estimated_total_tokens=estimated_total,
        effective_tpm=effective_tpm,
        effective_tpd=effective_tpd,
        requests_per_minute=requests_per_minute,
        requests_per_day=int(requests_per_day),
        seconds_per_request_at_tpm=seconds_per_request,
        seconds_for_100_requests=100.0 * seconds_per_request,
        shadow_cost_per_1000_answers_usd=shadow_cost,
    )


def plan_all(settings: Settings, presets: dict[str, RetrievalPreset]) -> list[dict[str, object]]:
    return [
        plan_for_preset(settings, preset).to_dict()
        for preset in presets.values()
    ]


def can_afford(settings: Settings, *, requests: int, tokens_per_request: int) -> bool:
    """Conservative yes/no for a single simulated day."""
    effective_tpd = max(1, int(settings.quota_tpd * (1.0 - settings.quota_safety_reserve)))
    return requests * tokens_per_request <= effective_tpd
