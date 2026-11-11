"""End-to-end pipeline tests with the deterministic stand-in.

These are the slowest tests because they use the real indexes, but they are the
only ones that prove the stages compose: retrieval -> evidence gate -> provider
-> verification -> CSV row.
"""

from __future__ import annotations

from pathlib import Path

from the_relay.core.pipeline import Relay
from the_relay.obs.results_csv import RESULTS_COLUMNS, ResultsWriter, read_results

FAST_STANDIN = {
    "base_latency_ms": 1.0,
    "latency_sigma": 0.01,
    "ttft_fraction": 0.1,
    "stream_chunk_ms": 0.0,
    "failure_rate": 0.0,
    "timeout_probability": 0.0,
}


def _results_path(settings) -> Path:
    return settings.resolved(settings.logs_dir) / "results.csv"


def _write(settings, *results) -> None:
    """Mirror what the experiment runner does with each user request.

    ``Relay.ask`` returns a result; one row is written by the harness that owns
    the experiment (the runner or the CLI), not by the pipeline itself. Writing
    here keeps the end-to-end test honest about that separation.
    """
    writer = ResultsWriter(_results_path(settings))
    for result in results:
        writer.append_result(result)


async def test_grounded_answer_is_cited_and_written_to_csv(settings_factory) -> None:
    settings = settings_factory()
    relay = Relay(settings, standin_overrides=FAST_STANDIN)
    try:
        result = await relay.ask(
            "What BLEU score did the big Transformer model achieve on the "
            "WMT 2014 English-to-German translation task?",
            preset_name="balanced",
            request_id="e2e-grounded",
        )
    finally:
        await relay.aclose()

    assert result.status == "ok"
    assert result.source == "stand-in"
    assert result.citations, "a grounded answer must carry at least one citation"
    assert "28.4" in result.answer
    assert result.tokens_in > 0
    assert result.tokens_out > 0

    _write(settings, result)
    rows = read_results(_results_path(settings))
    assert len(rows) == 1
    assert tuple(rows[0]) == RESULTS_COLUMNS
    assert rows[0]["request_id"] == "e2e-grounded"
    assert rows[0]["status"] == "ok"
    assert rows[0]["source"] == "stand-in"
    assert rows[0]["retrieval_setting"] == "balanced"


async def test_question_about_absent_topic_is_refused_without_a_model_call(
    settings_factory,
) -> None:
    settings = settings_factory()
    relay = Relay(settings, standin_overrides=FAST_STANDIN)
    try:
        result = await relay.ask(
            "How does the Transformer paper handle speech input?",
            preset_name="balanced",
            request_id="e2e-refusal",
        )
    finally:
        await relay.aclose()

    assert result.status == "ok"
    assert result.model_calls == 0, "a corpus-absence refusal must not call a model"
    assert "do not contain enough evidence" in result.answer
    assert result.citations == ()


async def test_exact_cache_serves_the_second_identical_question(settings_factory) -> None:
    settings = settings_factory(cache_enabled=True)
    relay = Relay(settings, standin_overrides=FAST_STANDIN)
    question = "How many GPUs were used to train the big Transformer model?"
    try:
        first = await relay.ask(question, request_id="e2e-cache-1")
        second = await relay.ask(question, request_id="e2e-cache-2")
    finally:
        await relay.aclose()

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.answer == first.answer
    _write(settings, first, second)
    rows = read_results(_results_path(settings))
    assert len(rows) == 2, "a cache hit is still one row per user request"


async def test_provider_rate_limit_maps_to_rate_limited_status(settings_factory) -> None:
    settings = settings_factory(retry_max_attempts=0)
    relay = Relay(
        settings,
        standin_overrides={**FAST_STANDIN, "rate_limit_on_calls": (1,)},
    )
    try:
        result = await relay.ask(
            "What BLEU score did the big Transformer achieve on WMT 2014?",
            request_id="e2e-rate-limit",
        )
    finally:
        await relay.aclose()

    assert result.status == "rate_limited"
    # The request body was sent, so the pipeline conservatively records the
    # estimated input tokens (traces mark them ``usage_estimated=True``). It
    # never invents output tokens for a failed call.
    assert result.tokens_in > 0
    assert result.tokens_out == 0
    _write(settings, result)
    rows = read_results(_results_path(settings))
    assert rows[0]["status"] == "rate_limited"
