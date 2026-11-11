"""Execute evaluation questions and load scenarios, and write the raw CSV.

TWO DIFFERENT KINDS OF RUN
--------------------------
1. **Quality runs (Table 3).** One question at a time, with a fresh quota
   manager per request. The point is to compare retrieval settings, not to see
   who gets rate-limited. Resetting the local quota is an explicit harness
   control and is stated in the report.
2. **Load runs (Tables 2a/2b).** One quota manager per scenario, shared by all
   requests in that scenario. This is where delay, queueing, and refusal are
   measured.

Both kinds write exactly one row per user request to ``logs/results.csv``.
Diagnostics that do not fit the 13 required columns go to JSONL traces.

WHY A FRESH RELAY PER QUALITY QUESTION?
---------------------------------------
The quota manager and stand-in are in-memory. Rebuilding them resets simulated
quota without pretending that a real provider reset early. The relay code path
is identical in both modes; only the harness lifecycle differs.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from the_relay.config import Settings
from the_relay.core.pipeline import Relay
from the_relay.eval.dataset import EvalQuestion
from the_relay.eval.grader import explain_grade
from the_relay.experiments.scenarios import Scenario
from the_relay.obs.results_csv import ResultsWriter
from the_relay.obs.traces import JsonlWriter, utc_now_iso


@dataclass
class RunSummary:
    """Small, report-friendly summary of one run."""

    run_id: str
    kind: str
    scenario: str
    started_at: str
    finished_at: str
    requests: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    grades: dict[str, int] = field(default_factory=dict)
    scheduler: dict[str, int] = field(default_factory=dict)
    provider_quota: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "scenario": self.scenario,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "requests": self.requests,
            "statuses": self.statuses,
            "grades": self.grades,
            "scheduler": self.scheduler,
            "provider_quota": self.provider_quota,
            "duration_s": round(self.duration_s, 1),
        }


def _new_run_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _answers_path(settings: Settings) -> Path:
    return settings.resolved(settings.logs_dir) / "eval_answers.jsonl"


def _summary_writer(settings: Settings) -> JsonlWriter:
    return JsonlWriter(settings.resolved(settings.logs_dir) / "experiment_runs.jsonl")


def _record_summary(settings: Settings, summary: RunSummary) -> None:
    _summary_writer(settings).append(summary.to_dict())


def _make_settings(settings: Settings, overrides: dict[str, Any]) -> Settings:
    """Clone the base settings with scenario overrides.

    ``model_copy(update=...)`` keeps validation and the env prefix behaviour,
    unlike mutating a shared settings object.
    """
    return settings.model_copy(update=overrides)


def _grade_and_write(
    *,
    question: EvalQuestion,
    result: Any,
    writer: ResultsWriter,
    answers: JsonlWriter,
) -> None:
    explanation = explain_grade(question, result.answer, result.status)
    result.answer_grade = explanation.grade
    writer.append_result(result)
    answers.append(
        {
            "request_id": result.request_id,
            "qid": question.qid,
            "question": question.question,
            "scenario": result.scenario,
            "retrieval_setting": result.retrieval_setting,
            "status": result.status,
            "answer_grade": explanation.grade,
            "grade_reason": explanation.reason,
            "matched_groups": list(explanation.matched_groups),
            "missing_groups": list(explanation.missing_groups),
            "cited": explanation.cited,
            "citations": list(result.citations),
            "evidence_level": result.evidence_level,
            "verification_passed": result.verification_passed,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "answer": result.answer,
            "timestamp": utc_now_iso(),
        }
    )


async def run_quality(
    settings: Settings,
    *,
    questions: list[EvalQuestion],
    preset_names: Iterable[str],
    run_id: str | None = None,
    standin_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Answer every question under every retrieval preset, in isolation."""
    run_id = run_id or _new_run_id("quality")
    started_at = utc_now_iso()
    started_mono = time.monotonic()
    writer = ResultsWriter(settings.resolved(settings.logs_dir) / "results.csv")
    answers = JsonlWriter(_answers_path(settings))

    summary = RunSummary(
        run_id=run_id,
        kind="quality",
        scenario="quality",
        started_at=started_at,
        finished_at="",
    )

    overrides = dict(standin_overrides or {})
    # Fast, deterministic stand-in for a quality run. This does not change the
    # pipeline; it only removes simulated waiting so the comparison is about
    # retrieval and generation, not about queue time.
    overrides.setdefault("base_latency_ms", 40.0)
    overrides.setdefault("failure_rate", 0.0)
    overrides.setdefault("timeout_probability", 0.0)

    for preset_name in preset_names:
        # Stand-in quality runs use a fresh Relay per question so that simulated
        # quota never becomes the variable being measured: Table 3 compares
        # retrieval settings, not admission control. A real provider is
        # different: its TPM window is shared, so resetting local quota per
        # question would just provoke real 429s. For Groq we therefore keep one
        # Relay (and one quota manager) per preset. This harness difference is
        # recorded in the report.
        shared_relay: Relay | None = None
        if settings.provider == "groq":
            shared_relay = Relay(
                _make_settings(settings, {"retrieval_preset": preset_name}),
            )
        for question in questions:
            relay = shared_relay or Relay(
                _make_settings(
                    settings,
                    {
                        "retrieval_preset": preset_name,
                        "cache_enabled": False,
                    },
                ),
                standin_overrides=overrides,
            )
            request_id = f"q-{preset_name}-{question.qid}-{run_id[-6:]}"
            result = await relay.ask(
                question.question,
                scenario="quality",
                request_id=request_id,
                preset_name=preset_name,
                # A stand-in question finishes in well under a second, so 60 s is a
            # generous safety net. A real Groq quality run is different: the
            # shared TPM window admits about three ~2.2k-token calls per minute,
            # so the fourth must wait ~60 s. A 60 s deadline then expires during
            # *normal* pacing and records a timeout that has nothing to do with
            # answer quality. Give the real run 120 s and state in the report
            # that its latency includes admission waiting.
            deadline_ms=120_000 if settings.provider == "groq" else 60_000,
                use_cache=False,
            )
            _grade_and_write(
                question=question,
                result=result,
                writer=writer,
                answers=answers,
            )
            summary.requests += 1
            summary.statuses[result.status] = (
                summary.statuses.get(result.status, 0) + 1
            )
            summary.grades[result.answer_grade] = (
                summary.grades.get(result.answer_grade, 0) + 1
            )
            if shared_relay is None:
                await relay.aclose()
        if shared_relay is not None:
            await shared_relay.aclose()

    summary.finished_at = utc_now_iso()
    summary.duration_s = time.monotonic() - started_mono
    _record_summary(settings, summary)
    return summary.to_dict()


async def run_scenario(
    settings: Settings,
    *,
    scenario: Scenario,
    questions: list[EvalQuestion],
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run one load shape with one shared quota manager."""
    run_id = run_id or _new_run_id(scenario.name)
    started_at = utc_now_iso()
    started_mono = time.monotonic()
    writer = ResultsWriter(settings.resolved(settings.logs_dir) / "results.csv")
    answers = JsonlWriter(_answers_path(settings))

    scenario_settings = _make_settings(settings, scenario.settings_overrides())
    relay = Relay(
        scenario_settings,
        standin_overrides=dict(scenario.standin_overrides) or None,
    )

    if scenario.preconsume_daily:
        # Fill the local day budget before the first request. This is the only
        # way to test the daily path without spending thousands of calls.
        await relay.quota.preconsume(
            tokens=scenario_settings.quota_tpd,
            requests=scenario_settings.quota_rpd,
        )

    by_qid = {question.qid: question for question in questions}
    chosen = [by_qid[qid] for qid in scenario.question_ids if qid in by_qid]
    if not chosen:
        raise ValueError(f"scenario {scenario.name!r} has no matching questions")

    semaphore = asyncio.Semaphore(max(1, scenario.concurrency))

    async def one(index: int) -> Any:
        await asyncio.sleep(index * scenario.interval_ms / 1000.0)
        question = chosen[index % len(chosen)]
        async with semaphore:
            request_id = f"s-{scenario.name}-{run_id[-6:]}-{index:02d}"
            result = await relay.ask(
                question.question,
                scenario=scenario.name,
                request_id=request_id,
                preset_name=scenario.preset,
                deadline_ms=scenario.deadline_ms,
                use_cache=False,
            )
            _grade_and_write(
                question=question,
                result=result,
                writer=writer,
                answers=answers,
            )
            return result

    tasks = [asyncio.create_task(one(index)) for index in range(scenario.requests)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    summary = RunSummary(
        run_id=run_id,
        kind="load",
        scenario=scenario.name,
        started_at=started_at,
        finished_at=utc_now_iso(),
        requests=scenario.requests,
        scheduler=relay.scheduler.stats(),
        provider_quota=relay.provider.quota_snapshot()
        if hasattr(relay.provider, "quota_snapshot")
        else {},
    )
    for item in results:
        if isinstance(item, Exception):
            summary.statuses["internal_error"] = (
                summary.statuses.get("internal_error", 0) + 1
            )
            continue
        # The per-request loop already wrote the graded row. Aggregate the same
        # objects here so Table 2b's outcome counts come from one source of
        # truth rather than a second pass over the CSV.
        summary.statuses[item.status] = summary.statuses.get(item.status, 0) + 1
        grade = item.answer_grade or "ungraded"
        summary.grades[grade] = summary.grades.get(grade, 0) + 1
    summary.duration_s = time.monotonic() - started_mono
    _record_summary(settings, summary)
    await relay.aclose()
    return summary.to_dict()


async def run_all_scenarios(
    settings: Settings,
    *,
    questions: list[EvalQuestion],
    scenario_names: Iterable[str],
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Run several load shapes sequentially, each with fresh quota."""
    from the_relay.experiments.scenarios import get_scenario

    outputs: list[dict[str, Any]] = []
    for name in scenario_names:
        outputs.append(
            await run_scenario(
                settings,
                scenario=get_scenario(name),
                questions=questions,
                run_id=run_id,
            )
        )
    return outputs
