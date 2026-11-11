r"""Command line entry point for The Relay.

USAGE (from the project root)
-----------------------------
    .\.venv\Scripts\python.exe -m the_relay.main ingest
    .\.venv\Scripts\python.exe -m the_relay.main ask "How does multi-head attention work?"
    .\.venv\Scripts\python.exe -m the_relay.main probe
    .\.venv\Scripts\python.exe -m the_relay.main eval
    .\.venv\Scripts\python.exe -m the_relay.main experiment --scenarios steady burst overload reduced_quota daily_exhausted
    .\.venv\Scripts\python.exe -m the_relay.main metrics

WHY A CLI?
----------
The assignment is graded on reproducible commands, not on how clever the code
looks. Every experiment is one command with explicit arguments, and every
command prints what it wrote. That makes the raw results auditable.

INTERACTIVE ASKS DO NOT GO INTO results.csv
-------------------------------------------
``ask`` is for a human. It writes traces but not an experiment row, because an
ungraded interactive question would inflate the denominators in the report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from the_relay.config import PROJECT_ROOT, Settings, get_settings, load_retrieval_config
from the_relay.core.pipeline import Relay
from the_relay.errors import ConfigurationError
from the_relay.eval.dataset import dataset_summary, load_questions
from the_relay.eval.metrics import (
    table_2a,
    table_2b,
    table_3,
    grade_counts,
)
from the_relay.experiments.budget_planner import plan_all
from the_relay.experiments.runner import run_all_scenarios, run_quality
from the_relay.obs.results_csv import read_results
from the_relay.rag.ingest import ingest_corpus, verify_corpus


def _settings(args: argparse.Namespace) -> Settings:
    """Build settings from defaults plus explicit CLI overrides."""
    overrides: dict[str, Any] = {}
    if getattr(args, "provider", None):
        overrides["provider"] = args.provider
    return Settings(**overrides)


def _questions_path(settings: Settings) -> Path:
    return settings.resolved(settings.evaluation_dir) / "questions.json"


def _load_questions(settings: Settings):
    return load_questions(_questions_path(settings))


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _sync_root_results(settings: Settings) -> Path:
    """Copy the canonical CSV to ``results.csv`` at the project root.

    The brief names the file ``results.csv`` without a directory. Keeping one
    canonical file in ``logs/`` and exporting it avoids two writers racing on
    the same path. The export is byte-for-byte identical.
    """
    source = settings.resolved(settings.logs_dir) / "results.csv"
    target = PROJECT_ROOT / "results.csv"
    if source.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return target


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def command_ingest(args: argparse.Namespace) -> int:
    settings = _settings(args)
    verification = verify_corpus(settings)
    for item in verification:
        state = "ok" if item["ok"] else "FAILED"
        print(f"{item['doc_id']}: {state} ({item['reason']})")
    if any(not item["ok"] for item in verification):
        print("Refusing to ingest a corpus that failed verification.", file=sys.stderr)
        return 1
    stats = ingest_corpus(settings)
    _print_json(stats)
    return 0


async def command_ask(args: argparse.Namespace) -> int:
    settings = _settings(args)
    relay = Relay(settings)
    if args.stream:
        def on_token(text: str, _elapsed_ms: float) -> None:
            print(text, end="", flush=True)
    else:
        on_token = None

    result = await relay.ask(
        args.question,
        preset_name=args.preset,
        deadline_ms=args.deadline_ms,
    )
    if args.stream and result.answer:
        print()
    print("=" * 70)
    print(f"status: {result.status}")
    print(f"evidence: {result.evidence_level} | verified: {result.verification_passed}")
    print(f"calls: {result.model_calls} | retries: {result.retries}")
    print(f"tokens: {result.tokens_in} in / {result.tokens_out} out")
    if result.first_token_ms is not None:
        print(f"first token: {result.first_token_ms:.0f} ms | total: {result.total_ms:.0f} ms")
    else:
        print(f"total: {result.total_ms:.0f} ms (no first token)")
    print(f"retrieval: {result.retrieval_setting} | citations: {list(result.citations)}")
    if result.refusal_reason:
        print(f"refusal reason: {result.refusal_reason}")
    print("-" * 70)
    if not args.stream:
        print(result.answer)
    await relay.aclose()
    return 0


async def command_probe(args: argparse.Namespace) -> int:
    """Send a small number of real requests and record what the service returns.

    This is intentionally tiny: the point is to observe headers and latency, not
    to exhaust the free tier. Without a key the command fails loudly and says no
    request was sent; it never substitutes stand-in numbers for real ones.
    """
    settings = _settings(args)
    if args.provider:
        settings = Settings(provider=args.provider)
    if not settings.groq_api_key:
        print(
            "GROQ_API_KEY is not set, so no real request was sent. Add it to .env "
            "or export it, then re-run `probe`.",
            file=sys.stderr,
        )
        return 2

    from the_relay.core.tokens import estimate_tokens
    from the_relay.obs.traces import JsonlWriter, utc_now_iso
    from the_relay.providers.groq import GroqProvider
    from the_relay.types import ModelRequest

    provider = GroqProvider(settings)
    writer = JsonlWriter(settings.resolved(settings.logs_dir) / "probe.jsonl")
    messages = [
        {"role": "system", "content": "Reply with the single word: ready."},
        {"role": "user", "content": "Are you ready?"},
    ]
    for index in range(args.requests):
        try:
            response = await provider.generate(
                ModelRequest(
                    messages=messages,
                    max_tokens=8,
                    temperature=0.0,
                    task="probe",
                    request_id=f"probe-{index}",
                )
            )
            record = {
                "timestamp": utc_now_iso(),
                "index": index,
                "status": "ok",
                "http_status": response.http_status,
                "first_token_ms": response.first_token_ms,
                "total_ms": response.total_ms,
                "text": response.text,
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "estimated": response.usage.estimated,
                },
                "rate_limit_headers": response.rate_limit_headers,
                "note": "one real request; limits observed from headers",
            }
            print(
                f"probe {index}: ok, http {response.http_status}, "
                f"ttft {response.first_token_ms}, total {response.total_ms:.0f} ms"
            )
        except Exception as exc:  # noqa: BLE001 - recorded, then surfaced
            record = {
                "timestamp": utc_now_iso(),
                "index": index,
                "status": "error",
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
                "rate_limit_headers": getattr(exc, "rate_limit_headers", {}),
                "note": "one real request that failed",
            }
            print(f"probe {index}: {type(exc).__name__}: {exc}", file=sys.stderr)
        writer.append(record)
    await provider.aclose()
    print(f"wrote {settings.resolved(settings.logs_dir) / 'probe.jsonl'}")
    return 0


async def command_eval(args: argparse.Namespace) -> int:
    settings = _settings(args)
    questions = _load_questions(settings)
    presets = args.presets or ["light", "balanced", "extensive"]
    retrieval = load_retrieval_config(settings)
    print("Evaluation set:", json.dumps(dataset_summary(questions)))
    print("Budget plan (documented limits, conservative estimate):")
    _print_json(plan_all(settings, retrieval.presets))
    summary = await run_quality(
        settings,
        questions=questions,
        preset_names=presets,
    )
    _print_json(summary)
    target = _sync_root_results(settings)
    print(f"results.csv exported to {target}")
    return 0


async def command_experiment(args: argparse.Namespace) -> int:
    settings = _settings(args)
    questions = _load_questions(settings)
    names = args.scenarios or [
        "steady",
        "burst",
        "overload",
        "reduced_quota",
        "daily_exhausted",
    ]
    if args.quality_first:
        print("Running quality pass first (fresh quota per question)...")
        quality = await run_quality(
            settings,
            questions=questions,
            preset_names=["light", "balanced", "extensive"],
        )
        _print_json(quality)
    summaries = await run_all_scenarios(
        settings,
        questions=questions,
        scenario_names=names,
    )
    _print_json(summaries)
    target = _sync_root_results(settings)
    print(f"results.csv exported to {target}")
    return 0


def command_metrics(args: argparse.Namespace) -> int:
    settings = _settings(args)
    rows = read_results(settings.resolved(settings.logs_dir) / "results.csv")
    if not rows:
        print(
            "No rows in logs/results.csv. Run `eval` or `experiment` first.",
            file=sys.stderr,
        )
        return 1
    payload = {
        "rows": len(rows),
        "grades": grade_counts(rows),
        "table_2a": table_2a(rows),
        "table_2b": table_2b(rows),
        "table_3": table_3(rows),
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"rows: {payload['rows']}")
        print("grades:", payload["grades"])
        print("\nTable 2a — latency by scenario")
        for row in payload["table_2a"]:
            print(
                f"  {row['scenario']:<18} n={row['requests_sent']:<3} "
                f"duration={row['duration_s']}s "
                f"offered={row['offered_rate_per_min']}/min "
                f"ttft p50/p95/p99={row['first_token']['p50']}/"
                f"{row['first_token']['p95']}/{row['first_token']['p99']} ms "
                f"total p50/p95/p99={row['total']['p50']}/"
                f"{row['total']['p95']}/{row['total']['p99']} ms"
            )
        print("\nTable 2b — outcomes by scenario")
        for row in payload["table_2b"]:
            print(
                f"  {row['scenario']:<18} ok={row['succeeded_pct']}% "
                f"rate_limited={row['rate_limited_pct']}% "
                f"timeout/givenup={row['timeout_or_given_up_pct']}% "
                f"retries={row['retries_per_request']} "
                f"peak_queue={row['peak_queue_length']} "
                f"tokens/answer={row['tokens_per_answer']} "
                f"cost/1000=${row['cost_per_1000_answers_usd']}"
            )
        print("\nTable 3 — retrieval settings")
        for row in payload["table_3"]:
            print(
                f"  {row['retrieval_setting']:<10} n={row['questions_asked']:<3} "
                f"correct+cited={row['correct_and_cited_pct']}% "
                f"wrong={row['wrong_pct']}% "
                f"refused_correctly={row['correctly_refused_pct']}% "
                f"refused_wrongly={row['refused_wrongly_pct']}% "
                f"tokens/answer={row['tokens_per_answer']} "
                f"ttft p50/p95={row['first_token']['p50']}/"
                f"{row['first_token']['p95']} ms"
            )
    if args.export:
        target = _sync_root_results(settings)
        print(f"results.csv exported to {target}")
    return 0


def command_reconcile(args: argparse.Namespace) -> int:
    """Check that model-call logs sum to the request rows in results.csv."""
    settings = _settings(args)
    results = read_results(settings.resolved(settings.logs_dir) / "results.csv")
    path = settings.resolved(settings.logs_dir) / "model_calls.jsonl"
    calls: list[dict[str, Any]] = []
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    calls.append(json.loads(line))

    by_call: dict[str, dict[str, int]] = {}
    for call in calls:
        bucket = by_call.setdefault(
            call["request_id"], {"tokens_in": 0, "tokens_out": 0, "calls": 0}
        )
        bucket["tokens_in"] += int(call.get("tokens_in") or 0)
        bucket["tokens_out"] += int(call.get("tokens_out") or 0)
        bucket["calls"] += 1

    mismatches: list[dict[str, Any]] = []
    for row in results:
        logged = by_call.get(row["request_id"])
        if logged is None:
            # A refusal or scope rejection never calls the model; the row's
            # token counts must therefore both be zero (or empty).
            if (row["tokens_in"] or "0") not in {"0", ""} or (row["tokens_out"] or "0") not in {"0", ""}:
                mismatches.append(
                    {"request_id": row["request_id"], "reason": "no calls but tokens recorded"}
                )
            continue
        if int(row["tokens_in"] or 0) != logged["tokens_in"]:
            mismatches.append(
                {
                    "request_id": row["request_id"],
                    "field": "tokens_in",
                    "csv": row["tokens_in"],
                    "calls": logged["tokens_in"],
                }
            )
        if int(row["tokens_out"] or 0) != logged["tokens_out"]:
            mismatches.append(
                {
                    "request_id": row["request_id"],
                    "field": "tokens_out",
                    "csv": row["tokens_out"],
                    "calls": logged["tokens_out"],
                }
            )
    print(
        json.dumps(
            {
                "results_rows": len(results),
                "model_calls": len(calls),
                "requests_with_calls": len(by_call),
                "mismatches": mismatches[:20],
                "mismatch_count": len(mismatches),
            },
            indent=2,
        )
    )
    return 0 if not mismatches else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="the_relay",
        description="The Relay: a grounded research assistant over three arXiv papers.",
    )
    parser.add_argument(
        "--provider",
        choices=["groq", "standin"],
        help="override RELAY_PROVIDER for this command",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_ingest = subparsers.add_parser("ingest", help="verify PDFs and build chunk indexes")
    p_ingest.set_defaults(func=command_ingest)

    p_ask = subparsers.add_parser("ask", help="ask one question interactively")
    p_ask.add_argument("question")
    p_ask.add_argument("--preset", choices=["light", "balanced", "extensive"])
    p_ask.add_argument("--stream", action="store_true")
    p_ask.add_argument("--deadline-ms", type=int, default=25_000)
    p_ask.set_defaults(func=command_ask, is_async=True)

    p_probe = subparsers.add_parser("probe", help="measure the real Groq service")
    p_probe.add_argument("--requests", type=int, default=1)
    p_probe.set_defaults(func=command_probe, is_async=True)

    p_eval = subparsers.add_parser("eval", help="answer the evaluation set under each preset")
    p_eval.add_argument(
        "--presets",
        nargs="+",
        choices=["light", "balanced", "extensive"],
        help="default: all three",
    )
    p_eval.set_defaults(func=command_eval, is_async=True)

    p_exp = subparsers.add_parser("experiment", help="run load scenarios")
    p_exp.add_argument(
        "--scenarios",
        nargs="+",
        choices=list(
            {
                "steady",
                "burst",
                "overload",
                "reduced_quota",
                "daily_exhausted",
            }
        ),
    )
    p_exp.add_argument(
        "--quality-first",
        action="store_true",
        help="also run the three-preset quality pass",
    )
    p_exp.set_defaults(func=command_experiment, is_async=True)

    p_metrics = subparsers.add_parser("metrics", help="recompute report tables")
    p_metrics.add_argument("--json", action="store_true")
    p_metrics.add_argument("--export", action="store_true")
    p_metrics.set_defaults(func=command_metrics)

    p_rec = subparsers.add_parser(
        "reconcile", help="check token reconciliation between CSV and model calls"
    )
    p_rec.set_defaults(func=command_reconcile, is_async=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "is_async", False):
            return asyncio.run(args.func(args))
        return args.func(args)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
