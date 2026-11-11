"""Typed data structures shared by every stage.

WHY A SEPARATE MODULE?
----------------------
The pipeline passes the same objects between retrieval, evidence, generation,
verification and instrumentation. Defining them once here means:

* the CSV writer and the trace writer cannot disagree about field names;
* a beginner can read one file and understand what data exists;
* tests can build a realistic object without running a model.

These are deliberately ``dataclasses`` rather than dictionaries. A typo in a
dictionary key fails silently at runtime; a typo in a dataclass field fails at
import time. On a system whose main output is measurements, silent failure is
the expensive kind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# The brief's exact status vocabulary. Do not add values: results.csv is
# graded against this list.
RequestStatus = Literal["ok", "rate_limited", "timeout", "error"]

# The brief's exact answer-grade vocabulary. Empty string is allowed only for
# requests that have not been graded (e.g. an interactive question). An empty
# grade is represented as ``None`` in Python and as an empty CSV cell.
AnswerGrade = Literal["correct", "wrong", "refused_correctly", "refused_wrongly"]

Source = Literal["real", "stand-in"]

EvidenceLevel = Literal["sufficient", "partial", "insufficient"]


@dataclass(frozen=True)
class RetrievedChunk:
    """One passage returned by the retrieval tool.

    Every field that could become a citation is preserved. The brief requires
    citations "where in the papers", so page and section are not decoration:
    they are the evidence a reviewer can check.
    """

    chunk_id: str
    doc_id: str
    title: str
    arxiv_id: str
    version: str
    page: int | None
    section: str | None
    text: str
    # Retrieval diagnostics. ``fused_score`` is RRF; ``relevance`` is the
    # normalised 0..1 score from the deterministic reranker (or the fused
    # score scaled to 0..1 when reranking is off).
    fused_score: float = 0.0
    relevance: float = 0.0
    rank: int = 0
    # Assigned just before prompting, e.g. "S1". Empty until then.
    source_id: str = ""

    @property
    def citation_label(self) -> str:
        """Human-readable source location used in prompts and reports."""
        bits = [self.title]
        if self.page is not None:
            bits.append(f"p. {self.page}")
        if self.section:
            bits.append(f"§{self.section}")
        return ", ".join(bits)


@dataclass(frozen=True)
class RetrievalResult:
    """Everything the retrieval stage produced, including its cost."""

    query: str
    preset: str
    chunks: tuple[RetrievedChunk, ...]
    duration_ms: float
    loops: int = 0
    reason: str = ""

    def context_tokens(self) -> int:
        """Rough size of the evidence that will enter the prompt."""
        from the_relay.core.tokens import estimate_tokens

        return sum(estimate_tokens(c.text) for c in self.chunks)


@dataclass(frozen=True)
class EvidenceAssessment:
    """The answer to: "may we generate from this evidence, and how completely?"

    ``level`` is not a measure of semantic similarity. It is a deterministic
    heuristic that combines query-term coverage, the top retrieval score, and
    whether the question asked for a kind of detail (a number, a comparison)
    that the retrieved text does not contain. It can be wrong, which is why
    the generation prompt must still disclose gaps and the verifier must still
    check citations.
    """

    level: EvidenceLevel
    reason: str
    coverage: float
    top1_score: float
    supporting_chunks: int
    documents: tuple[str, ...] = ()
    numeric_requested: bool = False
    numeric_found: bool = False


@dataclass
class ModelUsage:
    """Token usage for one model call.

    ``estimated`` is True when the provider did not return usage and we had to
    estimate locally. The report must not present estimated usage as provider
    billing data.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False

    def __post_init__(self) -> None:
        if self.total_tokens == 0:
            self.total_tokens = self.input_tokens + self.output_tokens


@dataclass
class ModelRequest:
    """Provider-neutral request.

    The orchestrator builds this; the Groq adapter and the stand-in both accept
    it. That is what makes the two backends interchangeable.
    """

    messages: list[dict[str, str]]
    max_tokens: int = 400
    temperature: float = 0.0
    # "generate" or "verify". Used for logging and for the stand-in's answer
    # strategy; the real adapter passes the same messages to the model.
    task: str = "generate"
    request_id: str = ""
    stage: str = "generation"


@dataclass
class ModelResponse:
    """Provider-neutral response, including every measurement we can capture."""

    text: str
    model_id: str
    source: Source
    usage: ModelUsage
    first_token_ms: float | None = None
    total_ms: float = 0.0
    http_status: int | None = None
    finish_reason: str | None = None
    rate_limit_headers: dict[str, str] = field(default_factory=dict)
    # Wall-clock UTC ISO time for the call. Used by the model-call log.
    started_at: str = ""
    ended_at: str = ""


@dataclass
class ModelCallRecord:
    """One record in ``logs/model_calls.jsonl``.

    One user request can produce several of these (generation, regeneration,
    optional verification, retries). The sum of their usage must reconcile
    with the request-level row in ``logs/results.csv``.
    """

    request_id: str
    call_id: str
    stage: str
    attempt: int
    model_id: str
    source: Source
    started_at: str
    ended_at: str
    duration_ms: float
    first_token_ms: float | None
    status: str
    tokens_in: int
    tokens_out: int
    total_tokens: int
    usage_estimated: bool
    http_status: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    retry_after_s: float | None = None
    rate_limit_headers: dict[str, str] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    """Outcome of checking an answer against the evidence that was retrieved."""

    passed: bool
    issues: tuple[str, ...]
    invalid_citations: tuple[str, ...]
    unsupported_claims: tuple[str, ...]
    claim_count: int
    supported_claim_count: int
    method: str = "deterministic"

    @property
    def support_ratio(self) -> float:
        if self.claim_count == 0:
            return 1.0
        return self.supported_claim_count / self.claim_count


@dataclass
class PipelineResult:
    """The complete result of one user request.

    This object is the single source of truth for the CSV row. The experiment
    runner may attach an ``answer_grade`` after grading; nothing else changes.
    """

    request_id: str
    scenario: str
    source: Source
    question: str
    start_time: str
    first_token_ms: float | None
    total_ms: float
    status: RequestStatus
    retries: int
    tokens_in: int
    tokens_out: int
    retrieval_setting: str
    answer_grade: AnswerGrade | None = None
    answer: str = ""
    citations: tuple[str, ...] = ()
    refusal_reason: str | None = None
    model_calls: int = 0
    evidence_level: str | None = None
    verification_passed: bool | None = None
    queue_wait_ms: float | None = None
    retrieval_ms: float | None = None
    generation_ms: float | None = None
    verification_ms: float | None = None
    retry_wait_ms: float | None = None
    error_type: str | None = None
    error_message: str | None = None
    cache_hit: bool = False

    def to_csv_row(self) -> dict[str, Any]:
        """Return exactly the 13 columns the brief requires, in order.

        A dict preserves insertion order in Python 3.7+, and the CSV writer
        writes the header explicitly from ``RESULTS_COLUMNS``, so order is
        guaranteed twice.
        """
        return {
            "request_id": self.request_id,
            "scenario": self.scenario,
            "source": self.source,
            "start_time": self.start_time,
            "first_token_ms": self.first_token_ms,
            "total_ms": self.total_ms,
            "status": self.status,
            "retries": self.retries,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "retrieval_setting": self.retrieval_setting,
            "answer_grade": self.answer_grade,
        }


@dataclass
class QuotaSnapshot:
    """A point-in-time view of quota accounting, written into traces."""

    requests_this_minute: int
    requests_today: int
    tokens_this_minute: int
    tokens_today: int
    in_flight: int
    reserved_tokens: int
    requests_remaining_today: int
    tokens_remaining_today: int
    minute_reset_in_s: float
    daily_reset_in_s: float
