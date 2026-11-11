"""The strict 13-column ``results.csv`` contract."""

from __future__ import annotations

import csv

import pytest

from the_relay.obs.results_csv import (
    RESULTS_COLUMNS,
    ResultsWriter,
    read_results,
    write_rows_atomic,
)


def _row(**overrides) -> dict[str, object]:
    row: dict[str, object] = {
        "request_id": "req_1",
        "scenario": "quality",
        "source": "stand-in",
        "start_time": "2026-11-11T00:00:00+00:00",
        "first_token_ms": 12.3,
        "total_ms": 45.6,
        "status": "ok",
        "retries": 0,
        "tokens_in": 100,
        "tokens_out": 20,
        "retrieval_setting": "balanced",
        "answer_grade": "correct",
    }
    row.update(overrides)
    return row


def test_header_is_exactly_the_required_columns(tmp_path) -> None:
    path = tmp_path / "results.csv"
    ResultsWriter(path, reset=True)
    with path.open(encoding="utf-8", newline="") as fh:
        header = next(csv.reader(fh))
    assert tuple(header) == RESULTS_COLUMNS


def test_required_schema_has_the_brief_s_exact_names() -> None:
    # The brief lists these names explicitly (docs/assignment_brief.txt p.4).
    # The frozen tuple is the authority; changing it would invalidate every
    # experiment, so this test is intentionally rigid.
    assert RESULTS_COLUMNS == (
        "request_id",
        "scenario",
        "source",
        "start_time",
        "first_token_ms",
        "total_ms",
        "status",
        "retries",
        "tokens_in",
        "tokens_out",
        "retrieval_setting",
        "answer_grade",
    )


def test_append_then_read_round_trip(tmp_path) -> None:
    path = tmp_path / "results.csv"
    writer = ResultsWriter(path, reset=True)
    writer.append_row(_row())
    rows = read_results(path)
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["answer_grade"] == "correct"


def test_missing_measurement_is_an_empty_cell_not_zero(tmp_path) -> None:
    path = tmp_path / "results.csv"
    writer = ResultsWriter(path, reset=True)
    writer.append_row(_row(first_token_ms=None))
    rows = read_results(path)
    assert rows[0]["first_token_ms"] == ""


def test_duplicate_request_id_is_rejected(tmp_path) -> None:
    writer = ResultsWriter(tmp_path / "results.csv", reset=True)
    writer.append_row(_row())
    with pytest.raises(ValueError, match="duplicate request_id"):
        writer.append_row(_row())


def test_unknown_status_is_rejected(tmp_path) -> None:
    writer = ResultsWriter(tmp_path / "results.csv", reset=True)
    with pytest.raises(ValueError, match="invalid status"):
        writer.append_row(_row(status="success"))


def test_unknown_source_is_rejected(tmp_path) -> None:
    writer = ResultsWriter(tmp_path / "results.csv", reset=True)
    with pytest.raises(ValueError, match="invalid source"):
        writer.append_row(_row(source="mock"))


def test_unknown_grade_is_rejected(tmp_path) -> None:
    writer = ResultsWriter(tmp_path / "results.csv", reset=True)
    with pytest.raises(ValueError, match="invalid answer_grade"):
        writer.append_row(_row(answer_grade="excellent"))


def test_missing_or_extra_column_is_rejected(tmp_path) -> None:
    writer = ResultsWriter(tmp_path / "results.csv", reset=True)
    bad = _row()
    bad.pop("tokens_out")
    with pytest.raises(ValueError, match="exactly the required columns"):
        writer.append_row(bad)

    extra = _row(request_id="req_2", latency_ms=10)
    with pytest.raises(ValueError, match="exactly the required columns"):
        writer.append_row(extra)


def test_wrong_header_on_existing_file_is_detected(tmp_path) -> None:
    path = tmp_path / "results.csv"
    path.write_text("request_id,scenario\nr1,q\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header does not match"):
        ResultsWriter(path)


def test_atomic_rewrite_resets_the_file(tmp_path) -> None:
    path = tmp_path / "results.csv"
    writer = ResultsWriter(path, reset=True)
    writer.append_row(_row(request_id="old"))
    write_rows_atomic(path, [_row(request_id="new")])
    rows = read_results(path)
    assert [r["request_id"] for r in rows] == ["new"]
