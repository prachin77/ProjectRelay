"""The deterministic stand-in must stream, cite and be reproducible."""

from __future__ import annotations

import pytest

from the_relay.core.prompts import build_generation_messages
from the_relay.providers.standin import StandInConfig, StandInProvider
from the_relay.types import EvidenceAssessment, ModelRequest


def _fast_config(**overrides) -> StandInConfig:
    values = {
        "base_latency_ms": 1.0,
        "latency_sigma": 0.01,
        "ttft_fraction": 0.1,
        "stream_chunk_ms": 0.0,
        "failure_rate": 0.0,
        "timeout_probability": 0.0,
    }
    values.update(overrides)
    return StandInConfig(**values)


def _messages(make_chunk):
    chunk = make_chunk(
        "We trained our models on one machine with 8 NVIDIA P100 GPUs. "
        "Training took 3.5 days.",
        section="6.1 Machine Translation",
    )
    assessment = EvidenceAssessment(
        level="sufficient",
        reason="test",
        coverage=1.0,
        top1_score=0.9,
        supporting_chunks=1,
    )
    return build_generation_messages(
        "How many GPUs were used to train the big model?",
        (chunk,),
        assessment,
    )


async def test_standin_is_deterministic(make_chunk) -> None:
    messages = _messages(make_chunk)
    provider_a = StandInProvider(_fast_config(seed=7))
    provider_b = StandInProvider(_fast_config(seed=7))
    request = ModelRequest(messages=messages, max_tokens=200)
    first = await provider_a.generate(request)
    second = await provider_b.generate(request)
    assert first.text == second.text
    assert first.source == "stand-in"


async def test_standin_emits_citation_and_uses_sources(make_chunk) -> None:
    provider = StandInProvider(_fast_config())
    response = await provider.generate(
        ModelRequest(messages=_messages(make_chunk), max_tokens=200)
    )
    assert "[S1]" in response.text
    assert "8" in response.text
    assert response.usage.output_tokens > 0


async def test_standin_streaming_invokes_the_callback(make_chunk) -> None:
    provider = StandInProvider(_fast_config())
    pieces: list[str] = []
    response = await provider.generate(
        ModelRequest(messages=_messages(make_chunk), max_tokens=200),
        on_token=lambda text, _elapsed: pieces.append(text),
    )
    assert pieces
    assert "".join(pieces).strip() == response.text.strip()
    assert response.first_token_ms is not None


async def test_scheduled_rate_limit_is_raised(make_chunk) -> None:
    provider = StandInProvider(_fast_config(rate_limit_on_calls=(1,)))
    with pytest.raises(Exception) as excinfo:
        await provider.generate(
            ModelRequest(messages=_messages(make_chunk), max_tokens=200)
        )
    assert "rate" in str(excinfo.value).lower()


async def test_missing_evidence_returns_a_refusal_not_a_guess() -> None:
    provider = StandInProvider(_fast_config())
    response = await provider.generate(
        ModelRequest(
            messages=[
                {"role": "system", "content": "x"},
                {"role": "user", "content": "QUESTION:\nWhat?\n\nEVIDENCE:\n\nINSTRUCTION: answer"},
            ],
            max_tokens=100,
        )
    )
    assert "not contain enough information" in response.text
    assert "[S" not in response.text
