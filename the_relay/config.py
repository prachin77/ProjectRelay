"""Central configuration for The Relay.

WHY THIS FILE EXISTS
--------------------
Every tunable number in the system lives here (or in ``config/*.yaml``), so an
experiment can be reproduced by quoting one file. The brief says reviewers
"recompute them from your raw file", which is only possible if the settings
that produced the raw file are recorded. Scattering constants through modules
would make that impossible.

TWO KINDS OF CONFIGURATION
--------------------------
1. **Environment-driven** (``Settings``): secrets and operational limits that
   change per machine or per account — the Groq key, the provider, quota
   numbers, concurrency. These come from ``.env`` / environment variables so
   that no secret is ever committed.
2. **File-driven** (``config/retrieval.yaml`` and
   ``config/standin_calibration.json``): experiment knobs that a human edits
   and re-runs — chunk size, top_k, reranking, simulated latency. These are
   version controlled because they are part of the experiment record.

HOW TO CHANGE A VALUE
---------------------
- Operational value: set ``RELAY_<NAME>`` in ``.env`` (see ``.env.example``).
- Retrieval preset: edit ``config/retrieval.yaml``.
- Stand-in behaviour: edit ``config/standin_calibration.json``.

COST / QUALITY / LATENCY IMPACT
-------------------------------
Almost every cost lever is set here:

* ``quota_*``              -> how hard we may push the provider, and therefore
                              how many requests get delayed or rejected.
* ``max_concurrency``      -> how many model calls are in flight at once.
* ``max_queue_depth``      -> how many waiting requests are tolerated before
                              honest rejection.
* ``request_deadline_ms``  -> how long a user waits before we give up honestly.
* ``max_output_tokens``     -> output-token cost per generation.
* ``max_tokens_per_request``-> hard token ceiling, including retries.
* ``verification_mode``    -> whether a second model call is spent on checking.
* retrieval presets        -> the direct cost/quality knob measured in Table 3.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = the directory that contains this package.
# ``Path(__file__).resolve().parents[1]`` walks up from
# ``the_relay/config.py`` to ``<project root>``.
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. Environment-driven settings
# ---------------------------------------------------------------------------
class Settings(BaseSettings):
    """Runtime settings loaded from the environment (and an optional ``.env``).

    Notes for a beginner
    --------------------
    ``BaseSettings`` reads each field from an environment variable named
    ``RELAY_<FIELD_NAME>`` in upper case. So ``quota_tpm`` is read from
    ``RELAY_QUOTA_TPM``. If the variable is missing, the default on the right
    of ``=`` is used. The defaults are deliberately safe: the system boots and
    runs offline with zero secrets.

    The one exception is ``GROQ_API_KEY``: it has an explicit alias without the
    ``RELAY_`` prefix because that is the conventional name used by Groq's own
    documentation and tooling.
    """

    model_config = SettingsConfigDict(
        env_prefix="RELAY_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # tolerate unrelated variables in the environment
    )

    # --- Provider selection ------------------------------------------------
    # "standin" is the safe default: it costs nothing and works offline.
    # "groq" requires GROQ_API_KEY and consumes real free-tier quota.
    provider: Literal["groq", "standin"] = "standin"

    # --- Groq --------------------------------------------------------------
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    # Verified against https://console.groq.com/docs/models on 2026-11-10.
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # gpt-oss is a reasoning model. On the free tier the reasoning tokens are
    # part of the completion budget, so a low effort setting keeps a short
    # evidence-grounded answer from being crowded out by hidden reasoning.
    groq_reasoning_effort: Literal["low", "medium", "high"] = "low"
    # Per-HTTP-request timeout. This is not the user-facing deadline; the
    # pipeline deadline is shorter and enforced around the whole request.
    groq_request_timeout_ms: int = 20_000

    # --- Quota -------------------------------------------------------------
    # Free-plan limits for openai/gpt-oss-120b, verified 2026-11-10 at
    # https://console.groq.com/docs/rate-limits.
    #
    # IMPORTANT: these are documented limits, not our account's observed
    # limits. The probe records the account's actual headers. Do not claim
    # these numbers were measured.
    quota_rpm: int = 30
    quota_tpm: int = 8_000
    quota_rpd: int = 1_000
    quota_tpd: int = 200_000

    # Fraction of each quota dimension we refuse to touch, so a short burst
    # does not instantly trip a hard 429. 0.15 = keep 15% in reserve.
    quota_safety_reserve: float = 0.15

    # Application-side concurrency cap. This is NOT a provider limit; Groq
    # does not document one. It exists because several slow calls can satisfy
    # RPM while still stalling the queue.
    max_concurrency: int = 5

    # --- Reliability -------------------------------------------------------
    # Bounded queue. When full, requests are rejected immediately rather than
    # accumulating in memory.
    max_queue_depth: int = 16
    max_queue_wait_ms: int = 15_000
    request_deadline_ms: int = 25_000

    # One controlled infrastructure retry (the master prompt's provisional
    # default). Retries consume quota and tokens, so this is deliberately low.
    retry_max_attempts: int = 1
    retry_base_delay_ms: int = 500
    retry_max_delay_ms: int = 8_000
    retry_jitter: bool = True

    # --- Generation --------------------------------------------------------
    max_output_tokens: int = 400
    temperature: float = 0.0  # deterministic: reproducible grading
    # Hard ceilings that protect the provider quota and the user's deadline.
    max_model_calls_per_request: int = 3
    max_tokens_per_request: int = 6_000
    max_regeneration_attempts: int = 1

    # Verification policy:
    #   "deterministic"   -> local citation/claim checks only (default)
    #   "conditional_llm" -> local checks always; an LLM check only when the
    #                        answer is complex or the local check is borderline
    #   "always_llm"      -> local checks + one LLM verification call
    #
    # The default avoids roughly doubling token cost on an 8k-TPM budget.
    verification_mode: Literal[
        "deterministic", "conditional_llm", "always_llm"
    ] = "deterministic"
    verification_max_attempts: int = 1

    # --- Retrieval ---------------------------------------------------------
    retrieval_preset: Literal["light", "balanced", "extensive"] = "balanced"
    # When the remaining daily token budget falls below this fraction, degrade
    # the retrieval tier (extensive -> balanced -> light). This is the honest
    # mechanism behind "who gets a weaker answer" in report heading 5.
    budget_pressure_threshold: float = 0.20
    degrade_under_pressure: bool = True

    # Evidence-sufficiency thresholds. These are engineering defaults, not
    # measured constants; they are tuned against the evaluation set and the
    # tuning result is recorded in the journal.
    evidence_min_top1_score: float = 0.20
    evidence_min_coverage: float = 0.34
    evidence_partial_coverage: float = 0.18
    evidence_max_loops: int = 2  # "at least one loop" required by the brief

    # --- Cache -------------------------------------------------------------
    # Exact cache only. It is safe because the key is the deterministic
    # normalised question plus every setting that could change the answer.
    # Semantic caching is deliberately not built (ADR-009).
    cache_enabled: bool = True
    cache_ttl_s: int = 24 * 60 * 60
    cache_max_entries: int = 512

    # --- Paths -------------------------------------------------------------
    documents_dir: Path = Path("documents")
    index_dir: Path = Path("indexes")
    logs_dir: Path = Path("logs")
    results_dir: Path = Path("results")
    config_dir: Path = Path("config")
    evaluation_dir: Path = Path("evaluation")

    # Bumped whenever the generation prompt changes. It is part of every cache
    # key, so editing the prompt cannot silently serve a stale answer.
    prompt_version: str = "p1"

    # --- Experiments -------------------------------------------------------
    simulation_seed: int = 42
    scenario: str = "interactive"

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def embedding_model_id(self) -> str:
        """Identifier of the local retrieval vectoriser, used in cache keys.

        We do not use a neural embedding model in this build (ADR-007). The
        value is still part of the cache key so that a future change to the
        vectoriser invalidates old answers.
        """
        return "tfidf-word-v1"

    def resolved(self, path: Path) -> Path:
        """Return ``path`` anchored at the project root (never the CWD).

        Why: the load generator may be started from any directory. Making all
        paths absolute relative to the project root removes a whole class of
        "works on my machine" bugs.
        """
        return path if path.is_absolute() else (PROJECT_ROOT / path)

    def ensure_dirs(self) -> None:
        """Create the output directories if they do not exist yet."""
        for d in (
            self.logs_dir,
            self.results_dir,
            self.index_dir,
            self.resolved(self.config_dir),
            self.resolved(self.evaluation_dir),
        ):
            self.resolved(d).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Safety helpers
    # ------------------------------------------------------------------
    def redacted(self) -> dict[str, Any]:
        """Return the settings as a dict with secrets removed.

        Used whenever we write configuration into a trace or a results file.
        Printing the key into a log would violate the assignment rule that the
        key must never be included in the submission.
        """
        data = self.model_dump()
        if data.get("groq_api_key"):
            data["groq_api_key"] = "***redacted***"
        return data


# ---------------------------------------------------------------------------
# 2. File-driven retrieval presets
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RetrievalPreset:
    """One of the three retrieval settings compared in Table 3.

    A preset bundles *how the corpus is split* with *how much of it is
    fetched*. They must be bundled because chunk size changes the index: you
    cannot fairly compare a 400-word chunk retrieval against a 200-word chunk
    retrieval using the same pre-built index.

    Fields
    ------
    name
        ``light`` / ``balanced`` / ``extensive``.
    description
        Human-readable intent, copied into reports and traces.
    chunk_words
        Target chunk size in words. The splitter snaps to paragraph/sentence
        boundaries, so the realised size fluctuates; the realised distribution
        is logged at ingestion time.
    overlap_words
        Words repeated between consecutive chunks. Overlap protects an answer
        that straddles a boundary, at the cost of embedding some text twice.
    top_k
        How many chunks are put into the generation prompt.
    rerank
        Whether the deterministic reranker re-orders the fused candidate pool.
    max_context_tokens
        Hard cap on evidence tokens passed to the model. This is the direct
        link between a retrieval setting and token cost.
    """

    name: str
    description: str
    chunk_words: int
    overlap_words: int
    top_k: int
    rerank: bool
    max_context_tokens: int

    @property
    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "chunk_words": self.chunk_words,
            "overlap_words": self.overlap_words,
            "top_k": self.top_k,
            "rerank": self.rerank,
            "max_context_tokens": self.max_context_tokens,
        }

    @property
    def fingerprint(self) -> str:
        """Short stable string used inside cache keys.

        Two answers generated under different retrieval settings must never
        share a cache entry, so the setting's identity has to be part of the
        key. A hash keeps the key short.
        """
        raw = json.dumps(self.as_dict, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:8]


@dataclass(frozen=True)
class RetrievalConfig:
    """The full contents of ``config/retrieval.yaml``."""

    fusion_pool: int
    rrf_k: int
    presets: dict[str, RetrievalPreset]

    def get(self, name: str) -> RetrievalPreset:
        if name not in self.presets:
            raise KeyError(
                f"unknown retrieval preset {name!r}; "
                f"available: {sorted(self.presets)}"
            )
        return self.presets[name]


def load_retrieval_config(settings: Settings) -> RetrievalConfig:
    """Parse ``config/retrieval.yaml`` into typed objects.

    Failing loudly here is deliberate. A typo in a preset name would otherwise
    silently fall back to a default and quietly corrupt a whole experiment.
    """
    path = settings.resolved(settings.config_dir) / "retrieval.yaml"
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    presets: dict[str, RetrievalPreset] = {}
    for name, body in raw["presets"].items():
        presets[name] = RetrievalPreset(
            name=name,
            description=" ".join(str(body.get("description", "")).split()),
            chunk_words=int(body["chunk_words"]),
            overlap_words=int(body["overlap_words"]),
            top_k=int(body["top_k"]),
            rerank=bool(body["rerank"]),
            max_context_tokens=int(body["max_context_tokens"]),
        )

    return RetrievalConfig(
        fusion_pool=int(raw.get("fusion_pool", 30)),
        rrf_k=int(raw.get("rrf_k", 60)),
        presets=presets,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached because reading ``.env`` on every call would be wasteful, and
    because tests sometimes monkeypatch the environment before first use.
    """
    settings = Settings()
    settings.ensure_dirs()
    return settings
