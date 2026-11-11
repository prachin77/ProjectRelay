"""Recompute every report table from the raw ``results.csv`` file.

WHY THIS MODULE EXISTS
----------------------
The brief says reviewers will recompute the tables from the raw file. If the
tables were assembled by hand, a single stale number would make the whole
submission unverifiable. Every function here takes the rows read by
``obs.results_csv.read_results`` and returns plain dictionaries that the report
generator prints.

PERCENTILES
-----------
Nearest-rank percentile: for ``n`` samples, the p-th percentile is the value at
0-based index ``ceil(p/100 * n) - 1``. With few samples this is easy to check by
hand, which is what a reviewer will do. It is not interpolated, because an
interpolated p99 from 12 requests would look more precise than the data allows.

MISSING IS NOT ZERO
-------------------
An empty ``first_token_ms`` cell means "no token was ever produced" (refused,
rate-limited, timed out). Those rows are excluded from first-token percentiles
and counted in the outcome table instead. They are never treated as 0 ms.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Iterable

# Groq's published paid-plan prices for openai/gpt-oss-120b, USD per million
# tokens, checked on 2026-11-10. The free tier itself has no per-token charge;
# this is a shadow price used only to make token consumption comparable.
PRICE_INPUT_PER_MILLION = 0.15
PRICE_OUTPUT_PER_MILLION = 0.60

# Rows are dict[str, str] exactly as read from CSV.
Row = dict[str, str]


def _number(cell: str | None) -> float | None:
    """Parse a CSV cell; an empty cell is missing, not zero."""
    if cell is None:
        return None
    text = str(cell).strip()
    if not text:
        return None
    return float(text)


def percentile(values: Iterable[float], p: float) -> float | None:
    """Nearest-rank percentile, or None when there are no values."""
    ordered = sorted(values)
    if not ordered:
        return None
    if not 0 < p <= 100:
        raise ValueError("percentile must be in (0, 100]")
    index = max(0, math.ceil(p / 100.0 * len(ordered)) - 1)
    return ordered[index]


def _percentile_triplet(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
    }


def _parse_time(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_seconds(rows: list[Row]) -> float | None:
    """Wall-clock span of a scenario, from request start to request end."""
    spans: list[tuple[float, float]] = []
    for row in rows:
        start = _parse_time(row.get("start_time", ""))
        total_ms = _number(row.get("total_ms")) or 0.0
        if start is None:
            continue
        start_s = start.timestamp()
        spans.append((start_s, start_s + total_ms / 1000.0))
    if not spans:
        return None
    return max(end for _start, end in spans) - min(start for start, _end in spans)


def _peak_concurrency(rows: list[Row]) -> int | None:
    """Peak overlapping requests derived from start time and total duration.

    This is a *lower bound* on the scheduler's queue occupancy: a request that
    waited inside the queue is included for its whole lifetime, but requests
    that were rejected before starting have no interval. The scheduler's own
    peak is written to the trace file; this derived value is what a reviewer can
    recompute from ``results.csv`` alone.
    """
    events: list[tuple[float, int]] = []
    for row in rows:
        start = _parse_time(row.get("start_time", ""))
        if start is None:
            continue
        start_s = start.timestamp()
        total_ms = _number(row.get("total_ms")) or 0.0
        events.append((start_s, 1))
        events.append((start_s + total_ms / 1000.0, -1))
    if not events:
        return None
    # Sort ends before starts at the same timestamp so a request that finishes
    # exactly when another starts does not look concurrent.
    events.sort(key=lambda pair: (pair[0], pair[1]))
    current = 0
    peak = 0
    for _timestamp, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def table_2a(rows: list[Row]) -> list[dict[str, Any]]:
    """Latency per load scenario (Table 2a).

    Only load rows are included. ``scenario=quality`` rows answer the same
    questions one at a time and would otherwise appear as a sixth "scenario",
    inflating the table with a population it is not describing.
    """
    grouped: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        if row.get("scenario") == "quality":
            continue
        grouped[row.get("scenario", "")].append(row)

    out: list[dict[str, Any]] = []
    for scenario, group in grouped.items():
        duration_s = _duration_seconds(group)
        first_tokens = [
            value
            for value in (_number(row.get("first_token_ms")) for row in group)
            if value is not None
        ]
        totals = [
            value
            for value in (_number(row.get("total_ms")) for row in group)
            if value is not None
        ]
        out.append(
            {
                "scenario": scenario,
                "source": sorted({row.get("source", "") for row in group}),
                "requests_sent": len(group),
                "duration_s": None if duration_s is None else round(duration_s, 1),
                "offered_rate_per_min": (
                    None
                    if not duration_s
                    else round(len(group) / (duration_s / 60.0), 1)
                ),
                "first_token": _percentile_triplet(first_tokens),
                "total": _percentile_triplet(totals),
            }
        )
    return out


def table_2b(rows: list[Row]) -> list[dict[str, Any]]:
    """Outcomes per load scenario (Table 2b)."""
    grouped: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        if row.get("scenario") == "quality":
            continue
        grouped[row.get("scenario", "")].append(row)

    out: list[dict[str, Any]] = []
    for scenario, group in grouped.items():
        n = len(group)
        statuses = Counter(row.get("status", "") for row in group)
        ok_rows = [row for row in group if row.get("status") == "ok"]
        token_totals = [
            ( _number(row.get("tokens_in")) or 0.0)
            + (_number(row.get("tokens_out")) or 0.0)
            for row in ok_rows
        ]
        retries = [_number(row.get("retries")) or 0.0 for row in group]
        tokens_per_answer = (
            sum(token_totals) / len(token_totals) if token_totals else None
        )
        # Shadow cost at published paid-plan rates. The free tier itself costs
        # nothing; this makes token consumption comparable across settings.
        cost_per_answer = (
            (
                (sum((_number(r.get("tokens_in")) or 0.0) for r in ok_rows)
                 * PRICE_INPUT_PER_MILLION)
                + (sum((_number(r.get("tokens_out")) or 0.0) for r in ok_rows)
                   * PRICE_OUTPUT_PER_MILLION)
            )
            / 1_000_000.0
            / len(ok_rows)
            if ok_rows
            else None
        )
        out.append(
            {
                "scenario": scenario,
                "source": sorted({row.get("source", "") for row in group}),
                "requests_sent": n,
                "succeeded_pct": round(100.0 * statuses.get("ok", 0) / n, 1),
                "rate_limited_pct": round(
                    100.0 * statuses.get("rate_limited", 0) / n, 1
                ),
                "timeout_or_given_up_pct": round(
                    100.0
                    * (statuses.get("timeout", 0) + statuses.get("error", 0))
                    / n,
                    1,
                ),
                "retries_per_request": round(sum(retries) / n, 2),
                "peak_queue_length": _peak_concurrency(group),
                "tokens_per_answer": (
                    None
                    if tokens_per_answer is None
                    else round(tokens_per_answer, 1)
                ),
                "cost_per_1000_answers_usd": (
                    None
                    if cost_per_answer is None
                    else round(cost_per_answer * 1000.0, 4)
                ),
            }
        )
    return out


def table_3(rows: list[Row]) -> list[dict[str, Any]]:
    """Quality and cost per retrieval setting (Table 3).

    ``correct`` already requires a citation (see ``eval/grader.py``), so the
    "correct and cited" column is the count of correct answers. The extra
    ``refused_wrongly`` count is reported too, because a setting that refuses
    answerable questions is not merely "not correct".
    """
    grouped: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in rows:
        # Table 3 is a retrieval-settings comparison, so it uses the quality
        # runs only. Load rows answer different questions under pressure and
        # would make the percentage depend on scenario design.
        if row.get("scenario") != "quality":
            continue
        grouped[
            (row.get("retrieval_setting", ""), row.get("source", ""))
        ].append(row)

    out: list[dict[str, Any]] = []
    for (setting, source), group in sorted(grouped.items()):
        n = len(group)
        grades = Counter(row.get("answer_grade", "") for row in group)
        correct = grades.get("correct", 0)
        wrong = grades.get("wrong", 0)
        refused_correctly = grades.get("refused_correctly", 0)
        refused_wrongly = grades.get("refused_wrongly", 0)
        ok_rows = [row for row in group if row.get("status") == "ok"]
        first_tokens = [
            value
            for value in (_number(row.get("first_token_ms")) for row in ok_rows)
            if value is not None
        ]
        totals = [
            value
            for value in (_number(row.get("total_ms")) for row in ok_rows)
            if value is not None
        ]
        token_totals = [
            (_number(row.get("tokens_in")) or 0.0)
            + (_number(row.get("tokens_out")) or 0.0)
            for row in ok_rows
        ]
        out.append(
            {
                "retrieval_setting": setting,
                "source": source,
                "questions_asked": n,
                "correct_and_cited_pct": round(100.0 * correct / n, 1),
                "wrong_pct": round(100.0 * wrong / n, 1),
                "correctly_refused_pct": round(100.0 * refused_correctly / n, 1),
                "refused_wrongly_pct": round(100.0 * refused_wrongly / n, 1),
                "tokens_per_answer": (
                    round(sum(token_totals) / len(token_totals), 1)
                    if token_totals
                    else None
                ),
                "first_token": _percentile_triplet(first_tokens),
                "total": _percentile_triplet(totals),
            }
        )
    return out


def scenario_rows(rows: list[Row], scenario: str) -> list[Row]:
    return [row for row in rows if row.get("scenario") == scenario]


def grade_counts(rows: list[Row]) -> dict[str, int]:
    """Counts for the four allowed grades, including zeroes."""
    counts = Counter(row.get("answer_grade", "") for row in rows)
    return {
        "correct": counts.get("correct", 0),
        "wrong": counts.get("wrong", 0),
        "refused_correctly": counts.get("refused_correctly", 0),
        "refused_wrongly": counts.get("refused_wrongly", 0),
        "ungraded": counts.get("", 0),
    }
