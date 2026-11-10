"""Central configuration for The Relay.

WHY THIS FILE EXISTS
--------------------
Every tunable number in the system lives here (or in ``config/*.yaml``), so that
an experiment can be reproduced by quoting one file. The brief says reviewers
"recompute them from your raw file", which is only possible if the settings
that produced the raw file are recorded. Scattering constants through modules
would make that impossible.

TWO KINDS OF CONFIGURATION
--------------------------
1. **Environment-driven** (``Settings``): secrets and operational limits that
   change per machine or per account -- the Groq key, the Redis URL, the
   quota numbers. These come from ``.env`` / environment variables so that no
   secret is ever committed.
2. **File-driven** (``config/retrieval.yaml``): experiment knobs that a human
   edits and re-runs -- chunk size, top_k, reranking. These are version
   controlled because they are *part of the experiment record*, not part of
   the deployment.

COST / QUALITY / LATENCY IMPACT
-------------------------------
This file itself does nothing at runtime, but almost every cost lever is set
here:

* ``quota_*``            -> how hard we are allowed to push the provider, and
                            therefore how many requests get delayed or dropped.
* ``request_deadline_ms`` -> how long a user waits before we give up honestly.
* ``semantic_cache_*``   -> how much we trust an approximate answer (quality
                            risk) in exchange for saved tokens (cost win).
* ``retrieval presets``  -> the direct cost/quality knob measured in Table 3.
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
    ``BaseSettings`` reads each field from an environment variable whose name
    is ``<env_prefix><FIELD_NAME>`` in upper case. So the field ``quota_tpm``
    is read from ``RELAY_QUOTA_TPM``. If the variable is missing, the default
    written on the right of ``=`` is used. That default is deliberately safe:
    the system boots and runs offline with zero secrets.
    """

    model_config = SettingsConfigDict(
        env_prefix="RELAY_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # tolerate unrelated variables in the environment
    )

    # --- Provider selection ------------------------------------------------
    provider: Literal["groq", "standin"] = "standin"

    # --- Groq --------------------------------------------------------------
    # The key is read from GROQ_API_KEY (no RELAY_ prefix) because that is the
    # conventional name and it keeps the value out of our own config namespace.
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # --- Redis ------------------------------------------------------------
    redis_url: str = "redis://127.0.0.1:6379/0"
    cache_enabled: bool = True
    # Semantic caching is opt-in: similarity is not equivalence, so this
    # feature must earn its place through measurement (see IMPLEMENTATION_PLAN D5).
    semantic_cache_enabled: bool = False
    semantic_similarity_threshold: float = 0.93
    semantic_min_margin: float = 0.02  # top1 must beat top2 by this much

    # Timeouts are deliberately tiny. A cache that is slow is worse than a
    # cache that is missing: it would add latency to every single request.
    redis_socket_timeout_ms: int = 50
    redis_connect_timeout_ms: int = 200

    # TTLs, in seconds. Rationale documented in IMPLEMENTATION_PLAN section 6.5.
    cache_ttl_exact_s: int = 7 * 24 * 3600
    cache_ttl_semantic_s: int = 7 * 24 * 3600
    cache_ttl_retrieval_s: int = 24 * 3600

    # --- Quota -------------------------------------------------------------
    # Verified from https://console.groq.com/docs/rate-limits on 2026-11-10 for
    # the free tier of openai/gpt-oss-120b. The probe run records what our own
    # account reports, which supersedes these defaults.
    quota_rpm: int = 30
    quota_tpm: int = 8_000
    quota_rpd: int = 1_000
    quota_tpd: int = 200_000

    max_concurrency: int = 4
    max_queue_depth: int = 32
    request_deadline_ms: int = 20_000

    # Fraction of each quota dimension we refuse to touch, so that short-term
    # bursts do not instantly trip a hard 429. 0.15 = keep 15% in reserve.
    quota_safety_reserve: float = 0.15

    # --- Generation --------------------------------------------------------
    max_output_tokens: int = 400
    temperature: float = 0.0  # deterministic: we want reproducible grading
    generation_max_retries: int = 2
    # Optional second LLM pass that checks the draft against the evidence.
    # OFF by default: on an 8k-TPM budget it roughly doubles token cost and
    # must demonstrably improve grounding before it is enabled.
    llm_verification_enabled: bool = False

    # --- Retrieval ---------------------------------------------------------
    retrieval_preset: str = "balanced"
    # When the remaining daily token budget falls below this fraction, degrade
    # the retrieval tier (extensive -> balanced -> light). This is the honest
    # mechanism behind "who gets a weaker answer" in report heading 5.
    budget_pressure_threshold: float = 0.20
    evidence_min_top1_score: float = 0.012
    evidence_max_loops: int = 2

    # --- Paths -------------------------------------------------------------
    documents_dir: Path = Path("documents")
    index_dir: Path = Path("indexes")
    logs_dir: Path = Path("logs")
    results_dir: Path = Path("results")
    config_dir: Path = Path("config")

    # Bumped whenever the generation prompt changes. It is part of every cache
    # key, so editing the prompt cannot silently serve a stale answer.
    prompt_version: str = "p1"

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def embedding_model_id(self) -> str:
        """Identifier of the local embedding model, used in cache keys."""
        return "BAAI/bge-small-en-v1.5"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reranker_model_id(self) -> str:
        return "Xenova/ms-marco-MiniLM-L-6-v2"

    def resolved(self, path: Path) -> Path:
        """Return ``path`` anchored at the project root (never the CWD).

        Why: the load generator may be started from any directory. Making all
        paths absolute relative to the project root removes a whole class of
        "works on my machine" bugs.
        """
        return path if path.is_absolute() else (PROJECT_ROOT / path)

    def ensure_dirs(self) -> None:
        """Create the output directories if they do not exist yet."""
        for d in (self.logs_dir, self.results_dir, self.resolved(self.config_dir)):
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

    A "preset" bundles *how the corpus is split* with *how much of it is
    fetched*. They must be bundled because chunk size changes the index: you
    cannot fairly compare a 700-word chunk retrieval against a 300-word chunk
    retrieval using the same pre-built index.

    Fields
    ------
    name
        ``light`` / ``balanced`` / ``extensive``.
    chunk_words
        Target chunk size in words *before* the splitter is applied. The
        splitter snaps to sentence/paragraph boundaries, so the realised size
        fluctuates; the realised distribution is logged at ingestion time.
    overlap_words
        Words repeated between consecutive chunks. Overlap protects an answer
        that straddles a boundary, at the cost of storing and embedding some
        text twice.
    top_k
        How many chunks are put into the generation prompt.
    rerank
        Whether a cross-encoder re-orders the fused candidate pool.
    """

    name: str
    description: str
    chunk_words: int
    overlap_words: int
    top_k: int
    rerank: bool

    @property
    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "chunk_words": self.chunk_words,
            "overlap_words": self.overlap_words,
            "top_k": self.top_k,
            "rerank": self.rerank,
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
