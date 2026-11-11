"""Table builders must use the right population.

A quality run answers one question at a time; a load run measures a scenario.
Mixing them would make Table 2a look like it had a sixth scenario and Table 3
depend on load design. These tests pin the boundary.

They also pin a reporting bug found in a canonical run: an out-of-scope refusal
was written with ``retrieval_setting="none"``, which created a phantom fourth
setting in Table 3 and silently removed those questions from every real preset.
"""

from __future__ import annotations

import pytest

from the_relay.eval.metrics import grade_counts, table_2a, table_2b, table_3


def _row(**overrides) -> dict[str, str]:
    row = {
        "request_id": "r1",
        "scenario": "steady",
        "source": "stand-in",
        "start_time": "2026-11-11T00:00:00+00:00",
        "first_token_ms": "100",
        "total_ms": "1000",
        "status": "ok",
        "retries": "0",
        "tokens_in": "1000",
        "tokens_out": "100",
        "retrieval_setting": "balanced",
        "answer_grade": "correct",
    }
    row.update(overrides)
    return row


def test_table_2a_excludes_quality_rows() -> None:
    rows = [_row(scenario="steady"), _row(scenario="quality", request_id="q1")]
    table = table_2a(rows)
    assert [r["scenario"] for r in table] == ["steady"]


def test_table_2b_excludes_quality_rows() -> None:
    rows = [_row(scenario="burst"), _row(scenario="quality", request_id="q1")]
    table = table_2b(rows)
    assert [r["scenario"] for r in table] == ["burst"]


def test_table_3_uses_only_quality_rows_and_separates_source() -> None:
    rows = [
        _row(request_id="q1", scenario="quality", source="stand-in"),
        _row(request_id="q2", scenario="quality", source="stand-in", answer_grade="wrong"),
        _row(request_id="q3", scenario="quality", source="real", answer_grade="correct"),
        _row(request_id="l1", scenario="steady", source="stand-in"),
    ]
    table = table_3(rows)
    keys = [(r["retrieval_setting"], r["source"]) for r in table]
    assert keys == [("balanced", "real"), ("balanced", "stand-in")]
    standin = next(r for r in table if r["source"] == "stand-in")
    assert standin["questions_asked"] == 2
    assert standin["correct_and_cited_pct"] == 50.0
    assert standin["wrong_pct"] == 50.0


def test_table_3_percentages_include_refusals_in_the_denominator() -> None:
    rows = [
        _row(request_id="q1", scenario="quality", answer_grade="correct"),
        _row(request_id="q2", scenario="quality", answer_grade="refused_wrongly"),
        _row(request_id="q3", scenario="quality", answer_grade="refused_correctly"),
        _row(request_id="q4", scenario="quality", answer_grade="wrong"),
    ]
    row = table_3(rows)[0]
    assert row["correct_and_cited_pct"] == 25.0
    assert row["refused_wrongly_pct"] == 25.0
    assert row["correctly_refused_pct"] == 25.0
    assert row["wrong_pct"] == 25.0


def test_grade_counts_includes_zeroes() -> None:
    counts = grade_counts([_row(answer_grade="correct")])
    assert counts == {
        "correct": 1,
        "wrong": 0,
        "refused_correctly": 0,
        "refused_wrongly": 0,
        "ungraded": 0,
    }


async def test_out_of_scope_refusal_records_the_requested_preset(settings_factory) -> None:
    """A scope rejection still belongs to the setting it was asked under."""
    from the_relay.core.pipeline import Relay

    settings = settings_factory()
    relay = Relay(
        settings,
        standin_overrides={
            "base_latency_ms": 1.0,
            "latency_sigma": 0.01,
            "ttft_fraction": 0.1,
            "stream_chunk_ms": 0.0,
            "failure_rate": 0.0,
            "timeout_probability": 0.0,
        },
    )
    try:
        result = await relay.ask(
            "What is the capital of France?",
            preset_name="extensive",
            request_id="scope-preset",
        )
    finally:
        await relay.aclose()

    assert result.evidence_level == "out_of_scope"
    assert result.retrieval_setting == "extensive"
    # Grading is the experiment harness's job; the pipeline leaves it empty.
    assert result.answer_grade is None
