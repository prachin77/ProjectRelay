"""The mandatory ``results.csv`` writer and reader.

THE CONTRACT
------------
The brief specifies exactly 13 columns and exactly one row per user request:

    request_id, scenario, source, start_time, first_token_ms, total_ms,
    status, retries, tokens_in, tokens_out, retrieval_setting, answer_grade

This module enforces that contract mechanically:

* the header is a frozen tuple;
* a row with an unknown or missing key raises;
* a duplicate ``request_id`` raises (one row per request);
* ``status`` and ``source`` must be from the brief's vocabulary;
* ``answer_grade`` must be from the brief's vocabulary or empty;
* a missing measurement is written as an **empty cell**, never as 0.

Why so strict? The brief says reviewers will recompute every table from this
file. A silently malformed row would make the report unrecomputable, which is
worse than a loud failure during an experiment.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable

from the_relay.types import PipelineResult

RESULTS_COLUMNS: tuple[str, ...] = (
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

ALLOWED_SOURCES = {"real", "stand-in"}
ALLOWED_STATUSES = {"ok", "rate_limited", "timeout", "error"}
ALLOWED_GRADES = {"correct", "wrong", "refused_correctly", "refused_wrongly", ""}


def _format_number(value: Any, *, decimals: int = 1) -> str:
    """Format a numeric CSV cell; ``None`` becomes an empty cell.

    Empty is the correct representation of "not measured". Writing 0 would make
    a missing first-token measurement look like an instantaneous model, which
    would corrupt every percentile.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Round to one decimal for milliseconds; keep integers clean.
        return str(round(value, decimals))
    return str(value)


class ResultsWriter:
    """Append rows to ``logs/results.csv`` with duplicate protection."""

    def __init__(self, path: Path, *, reset: bool = False) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seen_request_ids: set[str] = set()
        if reset or not self.path.exists():
            with self.path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=RESULTS_COLUMNS)
                writer.writeheader()
        else:
            # Refuse to append to a file with a different header. This catches
            # an accidental change to the required schema.
            with self.path.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.reader(fh)
                header = next(reader, [])
            if tuple(header) != RESULTS_COLUMNS:
                raise ValueError(
                    "results.csv header does not match the required schema; "
                    f"found {header!r}"
                )
            with self.path.open("r", encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    if row.get("request_id"):
                        self._seen_request_ids.add(row["request_id"])

    def append_result(self, result: PipelineResult) -> None:
        """Write one completed request.

        The ``PipelineResult`` is converted to the exact 13-column row. Extra
        diagnostic fields live in the JSONL traces, never here.
        """
        self.append_row(result.to_csv_row())

    def append_row(self, row: dict[str, Any]) -> None:
        """Validate and append one row.

        This is public so tests and the grader can write a row directly after
        attaching an answer grade.
        """
        missing = set(RESULTS_COLUMNS) - set(row)
        extra = set(row) - set(RESULTS_COLUMNS)
        if missing or extra:
            raise ValueError(
                "results.csv row must contain exactly the required columns; "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )

        request_id = str(row["request_id"])
        if not request_id:
            raise ValueError("request_id must not be empty")
        if request_id in self._seen_request_ids:
            raise ValueError(
                f"duplicate request_id {request_id!r}: results.csv must contain "
                "exactly one row per user request"
            )

        source = str(row["source"])
        if source not in ALLOWED_SOURCES:
            raise ValueError(f"invalid source {source!r}")
        status = str(row["status"])
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"invalid status {status!r}")
        grade = "" if row["answer_grade"] is None else str(row["answer_grade"])
        if grade not in ALLOWED_GRADES:
            raise ValueError(f"invalid answer_grade {grade!r}")

        formatted = {
            "request_id": request_id,
            "scenario": str(row["scenario"]),
            "source": source,
            "start_time": str(row["start_time"]),
            "first_token_ms": _format_number(row["first_token_ms"]),
            "total_ms": _format_number(row["total_ms"]),
            "status": status,
            "retries": _format_number(row["retries"]),
            "tokens_in": _format_number(row["tokens_in"]),
            "tokens_out": _format_number(row["tokens_out"]),
            "retrieval_setting": str(row["retrieval_setting"]),
            "answer_grade": grade,
        }

        with self.path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=RESULTS_COLUMNS)
            writer.writerow(formatted)
        self._seen_request_ids.add(request_id)


def read_results(path: Path) -> list[dict[str, str]]:
    """Read every row, returning strings exactly as they appear in the file.

    Metrics code converts to numbers explicitly so that an empty cell is
    distinguishable from a real zero.
    """
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if tuple(reader.fieldnames or ()) != RESULTS_COLUMNS:
            raise ValueError(
                "unexpected results.csv header: "
                f"{reader.fieldnames!r}"
            )
        return list(reader)


def count_rows(path: Path) -> int:
    return len(read_results(path))


def write_rows_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """Rewrite a results file from a list of rows (used by tests/resets).

    Kept separate from the append-only writer so an experiment cannot
    accidentally erase earlier runs. Only an explicit reset path calls this.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = ResultsWriter(path, reset=True)
    for row in rows:
        writer.append_row(row)
