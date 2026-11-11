"""The Relay orchestrator.

THE REQUEST LIFECYCLE
---------------------
1. Receive, normalise, assign a request id, start the trace.
2. Exact cache lookup (optional, zero tokens on hit).
3. Deterministic scope filter (zero tokens on an obvious refusal).
4. Enter the bounded queue / concurrency gate.
5. Retrieve passages locally; assess sufficiency; bounded re-retrieval loop.
6. If insufficient: refuse honestly, zero model calls.
7. Estimate tokens, acquire quota, call the model (streaming), reconcile usage.
8. Verify citations and claims deterministically.
9. At most one regeneration; then a transparent partial answer or refusal.
10. Cache a verified answer; write the CSV row and the JSONL traces.

WHAT THE ORCHESTRATOR MUST NOT DO
--------------------------------
It must not know whether the provider is real or simulated. It must not invent a
missing measurement (``None`` means missing). It must not retry without a bound.
It must not answer when the evidence gate says the library is insufficient.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from the_relay.config import (
    RetrievalConfig,
    RetrievalPreset,
    Settings,
    get_settings,
    load_retrieval_config,
)
from the_relay.core.cache import CacheEntry, ExactCache
from the_relay.core.evidence import assess_evidence, refusal_message as evidence_refusal
from the_relay.core.messages import (
    INTERNAL_ERROR,
    PROVIDER_UNAVAILABLE,
    QUEUE_FULL,
    TIMEOUT,
    daily_quota_message,
    provider_failure_message,
)


from the_relay.core.normalize import NormalizedQuestion, normalize_question
from the_relay.core.prompts import (
    build_generation_messages,
    build_regeneration_messages,
    build_verification_messages,
)
from the_relay.core.quota import QuotaManager
from the_relay.core.retry import RetryPolicy
from the_relay.core.scheduler import BoundedScheduler
from the_relay.core.scope import filter_scope, refusal_message as scope_refusal
from the_relay.core.tokens import estimate_messages_tokens
from the_relay.core.verify import (
    build_partial_answer,
    extract_citation_ids,
    normalise_citation_brackets,
    parse_llm_verification,
    resolve_citations,
    split_sentences,
    verify_answer,
)
from the_relay.errors import (
    DeadlineExceededError,
    ProviderError,
    ProviderTimeoutError,
    QuotaExceededError,
    QueueFullError,
    RateLimitError,
    RelayError,
)
from the_relay.obs.traces import (
    ModelCallWriter,
    TraceWriter,
    monotonic_ms,
    utc_now_iso,
)
from the_relay.providers.base import ModelProvider
from the_relay.providers.factory import build_provider
from the_relay.rag.ingest import corpus_version
from the_relay.rag.retrieval import RetrievalTool, assign_source_ids
from the_relay.rag.targeting import (
    absent_terms_from_corpus,
    detect_paper_target,
)
from the_relay.types import (
    ModelCallRecord,
    ModelRequest,
    ModelResponse,
    PipelineResult,
    RetrievalResult,
    RetrievedChunk,
    VerificationResult,
)


def quota_message(exc: QuotaExceededError) -> str:
    """Choose the honest message for a local quota rejection.

    A per-minute shortage and a daily exhaustion are different user situations.
    The daily message only mentions a reset time when the manager knows one
    (``retry_after_s``); it never invents a clock time.
    """
    text = str(exc).lower()
    if "daily" in text:
        hint: str | None = None
        if exc.retry_after_s and exc.retry_after_s < 86_400:
            minutes = max(1, round(exc.retry_after_s / 60))
            hint = f"{minutes} minutes"
        return daily_quota_message(reset_hint=hint)
    return provider_failure_message(kind="rate_limit")


@dataclass
class RequestState:
    """Mutable per-request counters.

    Why not instance attributes on ``Relay``? Because the relay handles several
    requests concurrently. Shared mutable counters would mix one user's tokens
    into another's row. A state object is created once per request and passed
    down explicitly.
    """

    request_start_mono: float = field(default_factory=monotonic_ms)
    tokens_in: int = 0
    tokens_out: int = 0
    model_calls: int = 0
    retries: int = 0
    retry_wait_ms: float = 0.0
    retrieval_ms: float = 0.0
    generation_ms: float = 0.0
    verification_ms: float = 0.0
    first_token_ms: float | None = None

    @property
    def tokens_used(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass
class _CallOutcome:
    """Result of one model call *with its retries*."""

    response: ModelResponse | None = None
    error: ProviderError | None = None


class Relay:
    """The application. Construct once, call ``ask`` many times concurrently."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        retrieval_config: RetrievalConfig | None = None,
        provider: ModelProvider | None = None,
        quota: QuotaManager | None = None,
        scheduler: BoundedScheduler | None = None,
        cache: ExactCache | None = None,
        standin_overrides: dict[str, Any] | None = None,
        trace_enabled: bool = True,
        model_call_log_enabled: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.retrieval_config = retrieval_config or load_retrieval_config(self.settings)
        self.provider = provider or build_provider(
            self.settings, standin_overrides=standin_overrides
        )
        self.quota = quota or QuotaManager.from_settings(self.settings)
        self.scheduler = scheduler or BoundedScheduler(
            max_concurrency=self.settings.max_concurrency,
            max_queue_depth=self.settings.max_queue_depth,
            max_queue_wait_ms=self.settings.max_queue_wait_ms,
        )
        self.cache = cache or ExactCache(
            enabled=self.settings.cache_enabled,
            ttl_s=self.settings.cache_ttl_s,
            max_entries=self.settings.cache_max_entries,
        )
        self.retrieval_tool = RetrievalTool(self.settings, self.retrieval_config)
        self.retry_policy = RetryPolicy(
            max_retries=self.settings.retry_max_attempts,
            base_delay_s=self.settings.retry_base_delay_ms / 1000.0,
            max_delay_s=self.settings.retry_max_delay_ms / 1000.0,
            jitter=self.settings.retry_jitter,
            seed=self.settings.simulation_seed,
        )
        self.trace = TraceWriter(
            self.settings.resolved(self.settings.logs_dir) / "request_traces.jsonl",
            enabled=trace_enabled,
        )
        self.model_call_log = ModelCallWriter(
            self.settings.resolved(self.settings.logs_dir) / "model_calls.jsonl",
            enabled=model_call_log_enabled,
        )
        # NOTE: this class deliberately has no results writer. One row per user
        # request is written by the experiment runner / CLI harness, which owns
        # grading and file lifecycle. Writing here as well would produce two
        # rows for one request and break the CSV contract.
        # Compute the corpus identity once per preset. It is part of every
        # cache key; doing it per request would re-hash the PDFs unnecessarily.
        self._corpus_versions = {
            name: corpus_version(self.settings, preset)
            for name, preset in self.retrieval_config.presets.items()
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def ask(
        self,
        question: str,
        *,
        scenario: str | None = None,
        request_id: str | None = None,
        preset_name: str | None = None,
        deadline_ms: int | None = None,
        on_token: Callable[[str, float], Awaitable[None] | None] | None = None,
        allow_degradation: bool = True,
        use_cache: bool = True,
        expected_grade: str | None = None,
    ) -> PipelineResult:
        """Answer one question and return a fully measured result.

        The method never raises for an ordinary operational failure. It returns
        a result with a status from the brief's vocabulary so the experiment
        runner can write exactly one row. Programming errors still propagate
        after being logged; that is intentional, because a silent bug in a
        measurement system is worse than a crash.
        """
        scenario = scenario or self.settings.scenario
        request_id = request_id or f"req_{uuid.uuid4().hex[:12]}"
        start_mono = monotonic_ms()
        start_time = utc_now_iso()
        deadline_ms = deadline_ms or self.settings.request_deadline_ms
        # UNIT CONTRACT: latency is measured in milliseconds (``monotonic_ms``)
        # but admission control and the scheduler use ``time.monotonic()``
        # seconds. Mixing the two silently produced a 90-million-second
        # deadline in a real run, which turned ordinary TPM pacing into
        # instant quota rejections. Keep this conversion explicit.
        deadline_mono = time.monotonic() + (deadline_ms / 1000.0)

        state = RequestState(request_start_mono=start_mono)
        normalized = normalize_question(question)
        preset, degraded_reason = self._select_preset(
            preset_name, allow_degradation=allow_degradation
        )
        result = self._new_result(
            request_id=request_id,
            scenario=scenario,
            question=question,
            start_time=start_time,
            preset=preset,
            expected_grade=expected_grade,
        )

        self.trace.log(
            request_id,
            "ingress",
            "request_received",
            question=question,
            scenario=scenario,
            deadline_ms=deadline_ms,
            preset=preset.name,
            degraded_preset=degraded_reason,
            config=self.settings.redacted(),
            source=self.provider.source,
            model_id=self.provider.model_id,
        )

        # --- 1. Exact cache ------------------------------------------------
        if use_cache and self.cache.enabled:
            cache_key = self._cache_key(normalized, preset)
            entry = self.cache.get(cache_key)
            self.trace.log(
                request_id,
                "cache",
                "cache_checked",
                hit=entry is not None,
                key=cache_key[:12],
            )
            if entry is not None:
                result.answer = entry.answer
                result.citations = entry.citation_ids
                result.evidence_level = entry.evidence_level
                result.verification_passed = entry.verification_passed
                result.cache_hit = True
                result.retrieval_setting = preset.name
                self._finalize(result, state, start_mono)
                self.trace.log(
                    request_id,
                    "egress",
                    "request_completed",
                    status=result.status,
                    cache_hit=True,
                    total_ms=result.total_ms,
                )
                return result

        # --- 2. Deterministic scope filter ---------------------------------
        decision = filter_scope(question)
        self.trace.log(
            request_id,
            "scope",
            "scope_filtered",
            decision=decision.status,
            reason=decision.reason,
            rule=decision.matched_rule,
            confidence=decision.confidence,
        )
        if decision.status == "out_of_scope":
            result.answer = scope_refusal(decision)
            result.refusal_reason = f"scope:{decision.matched_rule}"
            result.evidence_level = "out_of_scope"
            # Record the preset the request was *made under*, not "none".
            # Table 3 groups by this column; writing "none" created a phantom
            # fourth retrieval setting containing only out-of-scope refusals,
            # and silently removed those questions from every real preset.
            result.retrieval_setting = preset.name
            self._finalize(result, state, start_mono)
            self.trace.log(
                request_id,
                "egress",
                "request_refused",
                reason="out_of_scope",
                rule=decision.matched_rule,
                total_ms=result.total_ms,
                model_calls=0,
                tokens_in=0,
                tokens_out=0,
            )
            return result

        # --- 3. Queue + execute -------------------------------------------
        try:
            scheduled = await self.scheduler.run(
                lambda: self._process(
                    request_id=request_id,
                    question=question,
                    normalized=normalized,
                    preset=preset,
                    scenario=scenario,
                    start_time=start_time,
                    start_mono=start_mono,
                    deadline_mono=deadline_mono,
                    state=state,
                    on_token=on_token,
                    expected_grade=expected_grade,
                ),
                deadline_monotonic=deadline_mono,
            )
            result = scheduled.value
            result.queue_wait_ms = scheduled.queue_wait_ms
            self.trace.log(
                request_id,
                "queue",
                "queue_exited",
                queue_wait_ms=scheduled.queue_wait_ms,
                execution_ms=scheduled.execution_ms,
            )
        except QueueFullError as exc:
            result.status = "error"
            result.answer = QUEUE_FULL
            result.refusal_reason = "queue_full"
            result.error_type = "queue_full"
            result.error_message = str(exc)
            self.trace.log(
                request_id,
                "queue",
                "request_rejected",
                reason="queue_full",
                error_message=str(exc),
            )
        except DeadlineExceededError as exc:
            result.status = "timeout"
            result.answer = TIMEOUT
            result.refusal_reason = "deadline"
            result.error_type = "deadline"
            result.error_message = str(exc)
            self.trace.log(
                request_id,
                "deadline",
                "request_expired",
                reason="deadline",
                error_message=str(exc),
            )
        except QuotaExceededError as exc:
            result.status = "rate_limited"
            result.answer = quota_message(exc)
            result.refusal_reason = "quota"
            result.error_type = "quota"
            result.error_message = str(exc)
            self.trace.log(
                request_id,
                "admission",
                "request_rejected",
                reason="quota",
                error_message=str(exc),
            )
        except RelayError as exc:
            result.status = "error"
            result.answer = PROVIDER_UNAVAILABLE
            result.error_type = type(exc).__name__
            result.error_message = str(exc)[:500]
            self.trace.log(
                request_id,
                "egress",
                "request_rejected",
                reason="relay_error",
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
            )
        except Exception as exc:  # noqa: BLE001 - logged, then re-raised below
            # Never silently swallow an unexpected error. Log it, shape the
            # user-facing result, and re-raise so tests and the operator see it.
            result.status = "error"
            result.answer = INTERNAL_ERROR
            result.error_type = type(exc).__name__
            result.error_message = str(exc)[:500]
            self.trace.log(
                request_id,
                "egress",
                "request_rejected",
                reason="unexpected_error",
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
            )
            self._finalize(result, state, start_mono)
            self.trace.log(
                request_id,
                "egress",
                "request_completed",
                status=result.status,
                total_ms=result.total_ms,
                error_type=result.error_type,
            )
            raise

        self._finalize(result, state, start_mono)
        self.trace.log(
            request_id,
            "egress",
            "request_completed",
            status=result.status,
            total_ms=result.total_ms,
            first_token_ms=result.first_token_ms,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            model_calls=result.model_calls,
            retries=result.retries,
            evidence_level=result.evidence_level,
            verification_passed=result.verification_passed,
            cache_hit=result.cache_hit,
            error_type=result.error_type,
        )
        return result

    # ------------------------------------------------------------------
    # Core pipeline (runs inside the scheduler)
    # ------------------------------------------------------------------
    async def _process(
        self,
        *,
        request_id: str,
        question: str,
        normalized: NormalizedQuestion,
        preset: RetrievalPreset,
        scenario: str,
        start_time: str,
        start_mono: float,
        deadline_mono: float,
        state: RequestState,
        on_token: Callable[[str, float], Awaitable[None] | None] | None,
        expected_grade: str | None,
    ) -> PipelineResult:
        result = self._new_result(
            request_id=request_id,
            scenario=scenario,
            question=question,
            start_time=start_time,
            preset=preset,
            expected_grade=expected_grade,
        )
        result.retrieval_setting = preset.name

        # --- Paper targeting ------------------------------------------------
        # "The Moshi paper" / "H-Net" / "Transformer" tell us which PDF to open
        # first. This is a local, zero-token decision; it is logged so a wrong
        # target is auditable rather than invisible.
        target = detect_paper_target(question)
        doc_filter = target.doc_id if target is not None else None
        absent_topic_terms: tuple[str, ...] = ()
        if target is not None and target.explicit:
            absent_topic_terms = absent_terms_from_corpus(
                question,
                self.retrieval_tool.document_body_terms(preset.name, target.doc_id),
            )
        self.trace.log(
            request_id,
            "retrieval",
            "paper_targeted",
            doc_filter=doc_filter,
            explicit=bool(target and target.explicit),
            reason=target.reason if target else "no single paper named",
            absent_topic_terms=list(absent_topic_terms),
        )

        # --- Retrieval with a bounded evidence loop ------------------------
        retrieval: RetrievalResult | None = None
        assessment = None
        query_override: str | None = None
        loop = 0
        max_loops = self.settings.evidence_max_loops
        while loop <= max_loops:
            retrieval_started = monotonic_ms()
            self.trace.log(
                request_id,
                "retrieval",
                "retrieval_started",
                loop=loop,
                preset=preset.name,
                query=query_override or question,
                doc_filter=doc_filter,
            )
            retrieval = self.retrieval_tool.retrieve(
                question,
                preset.name,
                loop=loop,
                top_k_override=(
                    None if loop == 0 else min(preset.top_k + 2, 12)
                ),
                query_override=query_override,
                doc_filter=doc_filter,
            )
            state.retrieval_ms += monotonic_ms() - retrieval_started
            self.trace.log(
                request_id,
                "retrieval",
                "retrieval_completed",
                loop=loop,
                preset=preset.name,
                chunks=len(retrieval.chunks),
                duration_ms=retrieval.duration_ms,
                context_tokens=retrieval.context_tokens(),
                top_chunks=[
                    {
                        "chunk_id": chunk.chunk_id,
                        "doc_id": chunk.doc_id,
                        "page": chunk.page,
                        "section": chunk.section,
                        "relevance": round(chunk.relevance, 4),
                        "fused_score": round(chunk.fused_score, 6),
                    }
                    for chunk in retrieval.chunks
                ],
            )
            assessment = assess_evidence(
                question,
                retrieval.chunks,
                self.settings,
                absent_topic_terms=absent_topic_terms,
                named_paper=target.doc_id if target is not None else None,
            )
            self.trace.log(
                request_id,
                "evidence",
                "evidence_assessed",
                level=assessment.level,
                reason=assessment.reason,
                coverage=round(assessment.coverage, 4),
                top1_score=round(assessment.top1_score, 4),
                supporting_chunks=assessment.supporting_chunks,
                documents=list(assessment.documents),
                numeric_requested=assessment.numeric_requested,
                numeric_found=assessment.numeric_found,
                loop=loop,
                absent_topic_terms=list(absent_topic_terms),
            )
            if assessment.level != "insufficient":
                break
            if loop >= max_loops:
                break
            query_override = self.retrieval_tool.expanded_query(
                question, retrieval.chunks
            )
            loop += 1
            self.trace.log(
                request_id,
                "evidence",
                "loop_scheduled",
                next_loop=loop,
                expanded_query=query_override,
            )

        assert assessment is not None
        result.evidence_level = assessment.level
        if assessment.level == "insufficient":
            result.answer = evidence_refusal(assessment)
            result.refusal_reason = f"insufficient_evidence:{assessment.reason}"
            self.trace.log(
                request_id,
                "egress",
                "request_refused",
                reason="insufficient_evidence",
                detail=assessment.reason,
                model_calls=0,
                tokens_in=0,
                tokens_out=0,
            )
            return result

        assert retrieval is not None
        chunks = retrieval.chunks
        if not chunks:
            result.answer = evidence_refusal(assessment)
            result.refusal_reason = "no_passages"
            return result

        # --- Fit the prompt inside the per-request token budget ------------
        chunks, max_output_tokens = self._fit_prompt_budget(
            question, chunks, assessment, state
        )
        if not chunks or max_output_tokens <= 0:
            result.status = "error"
            result.answer = (
                "The retrieved evidence could not fit inside this request's "
                "token budget, so I did not call the model. Please ask a "
                "narrower question."
            )
            result.error_type = "token_budget"
            result.refusal_reason = "token_budget"
            return result

        chunks = assign_source_ids(chunks)
        messages = build_generation_messages(question, chunks, assessment)

        # --- Generation ----------------------------------------------------
        try:
            outcome = await self._call_with_retries(
                request_id=request_id,
                state=state,
                messages=messages,
                max_tokens=max_output_tokens,
                task="generate",
                stage="generation",
                deadline_mono=deadline_mono,
                on_token=on_token,
                preset_name=preset.name,
            )
        except QuotaExceededError as exc:
            result.status = "rate_limited"
            result.answer = quota_message(exc)
            result.refusal_reason = "quota"
            result.error_type = "quota"
            result.error_message = str(exc)
            self.trace.log(
                request_id,
                "admission",
                "request_rejected",
                reason="quota",
                error_message=str(exc),
            )
            return result

        if outcome.response is None:
            error = outcome.error or ProviderError("unknown provider failure")
            result.status = (
                "rate_limited"
                if isinstance(error, RateLimitError)
                else "timeout"
                if isinstance(error, ProviderTimeoutError)
                else "error"
            )
            result.answer = provider_failure_message(kind=error.kind)
            result.refusal_reason = f"provider:{error.kind}"
            result.error_type = error.kind
            result.error_message = str(error)[:500]
            self.trace.log(
                request_id,
                "egress",
                "request_rejected",
                reason="provider_failure",
                error_type=error.kind,
                error_message=str(error)[:500],
                http_status=error.http_status,
            )
            return result

        answer = normalise_citation_brackets(outcome.response.text.strip())
        if not answer:
            answer = (
                "The model returned an empty answer. I have not filled the gap "
                "with an invented one."
            )

        # --- Verification --------------------------------------------------
        verification = self._verify(request_id, state, answer, chunks)
        self.trace.log(
            request_id,
            "verification",
            "verification_completed",
            passed=verification.passed,
            method=verification.method,
            claim_count=verification.claim_count,
            supported_claim_count=verification.supported_claim_count,
            invalid_citations=list(verification.invalid_citations),
            unsupported_claims=list(verification.unsupported_claims),
            issues=list(verification.issues),
        )

        # Optional LLM verification. It is an *additional* call whose cost must
        # be justified by an A/B experiment; default is deterministic only.
        if self._needs_llm_verification(verification, assessment, answer):
            verification = await self._run_llm_verification(
                request_id=request_id,
                state=state,
                question=question,
                answer=answer,
                chunks=chunks,
                deadline_mono=deadline_mono,
                baseline=verification,
            )

        # --- One bounded regeneration --------------------------------------
        regeneration_attempts = 0
        while (
            not verification.passed
            and regeneration_attempts < self.settings.max_regeneration_attempts
            and state.model_calls < self.settings.max_model_calls_per_request
            and state.tokens_used < self.settings.max_tokens_per_request
        ):
            regeneration_attempts += 1
            self.trace.log(
                request_id,
                "generation",
                "regeneration_scheduled",
                attempt=regeneration_attempts,
                issues=list(verification.issues),
            )
            regen_messages = build_regeneration_messages(
                question, chunks, assessment, answer, verification.issues
            )
            remaining_budget = (
                self.settings.max_tokens_per_request - state.tokens_used
            )
            regen_max_tokens = min(max_output_tokens, max(120, remaining_budget // 3))
            try:
                regen_outcome = await self._call_with_retries(
                    request_id=request_id,
                    state=state,
                    messages=regen_messages,
                    max_tokens=regen_max_tokens,
                    task="generate",
                    stage="regeneration",
                    deadline_mono=deadline_mono,
                    on_token=on_token,
                    preset_name=preset.name,
                )
            except QuotaExceededError:
                self.trace.log(
                    request_id,
                    "generation",
                    "regeneration_skipped",
                    reason="quota_exhausted",
                )
                break
            if regen_outcome.response is None or not regen_outcome.response.text.strip():
                break
            answer = normalise_citation_brackets(regen_outcome.response.text.strip())
            verification = self._verify(request_id, state, answer, chunks)
            self.trace.log(
                request_id,
                "verification",
                "verification_completed",
                passed=verification.passed,
                method=verification.method,
                claim_count=verification.claim_count,
                supported_claim_count=verification.supported_claim_count,
                invalid_citations=list(verification.invalid_citations),
                unsupported_claims=list(verification.unsupported_claims),
                issues=list(verification.issues),
                attempt=regeneration_attempts,
            )

        if not verification.passed:
            answer = build_partial_answer(answer, chunks)
            self.trace.log(
                request_id,
                "verification",
                "partial_answer_returned",
                reasons=list(verification.issues),
            )

        citation_ids: list[str] = []
        for sentence in split_sentences(answer):
            for source_id in extract_citation_ids(sentence):
                if source_id not in citation_ids:
                    citation_ids.append(source_id)
        result.answer = answer
        result.citations = tuple(citation_ids)
        result.verification_passed = verification.passed

        # --- Cache only verified, non-refusal answers ----------------------
        if self.cache.enabled and verification.passed:
            cache_key = self._cache_key(normalized, preset)
            self.cache.put(
                cache_key,
                CacheEntry(
                    answer=answer,
                    citation_ids=tuple(citation_ids),
                    citation_metadata=tuple(resolve_citations(citation_ids, chunks)),
                    evidence_level=assessment.level,
                    verification_passed=True,
                    created_at=time.monotonic(),
                    expires_at=time.monotonic() + self.settings.cache_ttl_s,
                    preset=preset.name,
                    model_id=self.provider.model_id,
                    prompt_version=self.settings.prompt_version,
                    corpus_version=self._corpus_versions[preset.name],
                ),
            )
        return result

    # ------------------------------------------------------------------
    # Model call with bounded retries and quota reconciliation
    # ------------------------------------------------------------------
    async def _call_with_retries(
        self,
        *,
        request_id: str,
        state: RequestState,
        messages: list[dict[str, str]],
        max_tokens: int,
        task: str,
        stage: str,
        deadline_mono: float,
        on_token: Callable[[str, float], Awaitable[None] | None] | None,
        preset_name: str,
    ) -> _CallOutcome:
        """Make one logical model call, with at most the configured retries.

        Each *attempt* is a separate model call, quota reservation and
        model-call log record. The request-level CSV sums all of them.
        """
        attempts = 0
        while True:
            if state.model_calls >= self.settings.max_model_calls_per_request:
                return _CallOutcome(
                    error=ProviderError(
                        "per-request model-call budget exhausted",
                        kind="call_budget",
                        retryable=False,
                    )
                )
            estimated_input = estimate_messages_tokens(messages)
            estimated_total = estimated_input + max_tokens
            if state.tokens_used + estimated_total > self.settings.max_tokens_per_request:
                return _CallOutcome(
                    error=ProviderError(
                        "per-request token budget exhausted",
                        kind="token_budget",
                        retryable=False,
                    )
                )

            try:
                ticket = await self.quota.acquire(
                    estimated_total, deadline_monotonic=deadline_mono
                )
            except QuotaExceededError:
                raise

            call_id = f"call_{uuid.uuid4().hex[:12]}"
            call_started_mono = monotonic_ms()
            call_started_at = utc_now_iso()
            self.trace.log(
                request_id,
                stage,
                "admission_decided",
                admitted=True,
                estimated_tokens=estimated_total,
                quota=self.quota.to_dict(),
            )
            self.trace.log(
                request_id,
                stage,
                "model_call_started",
                call_id=call_id,
                attempt=attempts,
                model_id=self.provider.model_id,
                source=self.provider.source,
                task=task,
                estimated_input_tokens=estimated_input,
                max_output_tokens=max_tokens,
            )

            def token_callback(text: str, elapsed_ms: float) -> Any:
                # The first callback is the user-visible first token. Measure it
                # from the *request* start, not the call start, so table 2a
                # includes queueing and retrieval time.
                if state.first_token_ms is None:
                    state.first_token_ms = monotonic_ms() - state.request_start_mono
                    self.trace.log(
                        request_id,
                        stage,
                        "first_token_received",
                        call_id=call_id,
                        first_token_ms=state.first_token_ms,
                    )
                if on_token is not None:
                    return on_token(text, elapsed_ms)
                return None

            try:
                response = await self.provider.generate(
                    ModelRequest(
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=self.settings.temperature,
                        task=task,
                        request_id=request_id,
                        stage=stage,
                    ),
                    token_callback,
                )
                await self.quota.release(ticket, response.usage.total_tokens)
                state.tokens_in += response.usage.input_tokens
                state.tokens_out += response.usage.output_tokens
                state.model_calls += 1
                elapsed_ms = monotonic_ms() - call_started_mono
                if stage == "verification":
                    state.verification_ms += elapsed_ms
                else:
                    state.generation_ms += elapsed_ms
                if state.first_token_ms is None and response.first_token_ms is not None:
                    # Fallback when the provider did not invoke the callback
                    # (e.g. a non-streaming response).
                    state.first_token_ms = (
                        call_started_mono - state.request_start_mono
                        + response.first_token_ms
                    )
                self._log_model_call(
                    request_id=request_id,
                    call_id=call_id,
                    stage=stage,
                    attempt=attempts,
                    response=response,
                    status="ok",
                    tokens_in=response.usage.input_tokens,
                    tokens_out=response.usage.output_tokens,
                    usage_estimated=response.usage.estimated,
                    error_type=None,
                    error_message=None,
                    retry_after_s=None,
                    preset_name=preset_name,
                    max_tokens=max_tokens,
                )
                self.trace.log(
                    request_id,
                    stage,
                    "model_call_completed",
                    call_id=call_id,
                    attempt=attempts,
                    status="ok",
                    duration_ms=response.total_ms,
                    first_token_ms=response.first_token_ms,
                    tokens_in=response.usage.input_tokens,
                    tokens_out=response.usage.output_tokens,
                    http_status=response.http_status,
                    rate_limit_headers=response.rate_limit_headers,
                )
                return _CallOutcome(response=response)
            except ProviderError as exc:
                # Failed calls are conservatively charged the estimated input
                # tokens. The provider may not have processed them, but we do
                # not have a better measurement; over-counting is safer than
                # pretending a failed call was free.
                await self.quota.release(ticket, estimated_input)
                state.tokens_in += estimated_input
                state.model_calls += 1
                elapsed_ms = monotonic_ms() - call_started_mono
                if stage == "verification":
                    state.verification_ms += elapsed_ms
                else:
                    state.generation_ms += elapsed_ms
                self._log_model_call(
                    request_id=request_id,
                    call_id=call_id,
                    stage=stage,
                    attempt=attempts,
                    response=None,
                    status="error",
                    tokens_in=estimated_input,
                    tokens_out=0,
                    usage_estimated=True,
                    error_type=exc.kind,
                    error_message=str(exc)[:500],
                    retry_after_s=exc.retry_after_s,
                    preset_name=preset_name,
                    max_tokens=max_tokens,
                    http_status=exc.http_status,
                )
                remaining_ms = deadline_mono - monotonic_ms()
                decision = self.retry_policy.decide(
                    exc,
                    retries_so_far=state.retries,
                    remaining_ms=remaining_ms,
                    estimated_tokens_available=(
                        state.tokens_used + estimated_total
                        <= self.settings.max_tokens_per_request
                    ),
                )
                self.trace.log(
                    request_id,
                    stage,
                    "retry_scheduled" if decision.should_retry else "model_call_failed",
                    call_id=call_id,
                    attempt=attempts,
                    error_type=exc.kind,
                    error_message=str(exc)[:500],
                    retry_after_s=exc.retry_after_s,
                    retry_delay_s=decision.delay_s,
                    retry_reason=decision.reason,
                    remaining_ms=remaining_ms,
                )
                if not decision.should_retry:
                    return _CallOutcome(error=exc)
                state.retries += 1
                state.retry_wait_ms += decision.delay_s * 1000.0
                await asyncio.sleep(decision.delay_s)
                attempts += 1
                continue
            except asyncio.CancelledError:
                await self.quota.cancel(ticket)
                self.trace.log(
                    request_id,
                    stage,
                    "model_call_cancelled",
                    call_id=call_id,
                    attempt=attempts,
                )
                raise
            except Exception as exc:  # noqa: BLE001
                await self.quota.cancel(ticket)
                self._log_model_call(
                    request_id=request_id,
                    call_id=call_id,
                    stage=stage,
                    attempt=attempts,
                    response=None,
                    status="error",
                    tokens_in=0,
                    tokens_out=0,
                    usage_estimated=False,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                    retry_after_s=None,
                    preset_name=preset_name,
                    max_tokens=max_tokens,
                )
                self.trace.log(
                    request_id,
                    stage,
                    "model_call_failed",
                    call_id=call_id,
                    attempt=attempts,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                )
                raise

    # ------------------------------------------------------------------
    # Verification helpers
    # ------------------------------------------------------------------
    def _verify(
        self,
        request_id: str,
        state: RequestState,
        answer: str,
        chunks: tuple[RetrievedChunk, ...],
    ) -> VerificationResult:
        started = monotonic_ms()
        self.trace.log(
            request_id,
            "verification",
            "verification_started",
            method="deterministic",
            chunks=len(chunks),
        )
        result = verify_answer(answer, chunks)
        state.verification_ms += monotonic_ms() - started
        return result

    def _needs_llm_verification(
        self,
        verification: VerificationResult,
        assessment: Any,
        answer: str,
    ) -> bool:
        mode = self.settings.verification_mode
        if mode == "deterministic":
            return False
        if mode == "always_llm":
            return True
        # conditional_llm: spend the second call only where risk is highest.
        return (
            not verification.passed
            or assessment.level == "partial"
            or len(answer.split()) > 90
            or verification.support_ratio < 1.0
        )

    async def _run_llm_verification(
        self,
        *,
        request_id: str,
        state: RequestState,
        question: str,
        answer: str,
        chunks: tuple[RetrievedChunk, ...],
        deadline_mono: float,
        baseline: VerificationResult,
    ) -> VerificationResult:
        if state.model_calls >= self.settings.max_model_calls_per_request:
            baseline.issues = baseline.issues + (
                "LLM verification skipped: model-call budget exhausted",
            )
            return baseline
        messages = build_verification_messages(question, answer, chunks)
        try:
            outcome = await self._call_with_retries(
                request_id=request_id,
                state=state,
                messages=messages,
                max_tokens=200,
                task="verify",
                stage="verification",
                deadline_mono=deadline_mono,
                on_token=None,
                preset_name="n/a",
            )
        except QuotaExceededError:
            baseline.issues = baseline.issues + (
                "LLM verification skipped: quota exhausted",
            )
            return baseline
        if outcome.response is None:
            baseline.issues = baseline.issues + ("LLM verification call failed",)
            return baseline
        llm_result = parse_llm_verification(outcome.response.text)
        # Combine: both checks must pass. Report which method failed.
        passed = baseline.passed and llm_result.passed
        issues = tuple(dict.fromkeys(baseline.issues + llm_result.issues))
        return VerificationResult(
            passed=passed,
            issues=issues,
            invalid_citations=tuple(
                dict.fromkeys(baseline.invalid_citations + llm_result.invalid_citations)
            ),
            unsupported_claims=tuple(
                dict.fromkeys(baseline.unsupported_claims + llm_result.unsupported_claims)
            ),
            claim_count=max(baseline.claim_count, llm_result.claim_count),
            supported_claim_count=min(
                baseline.supported_claim_count, llm_result.supported_claim_count
            ),
            method="deterministic+llm",
        )

    # ------------------------------------------------------------------
    # Budget, cache and result helpers
    # ------------------------------------------------------------------
    def _fit_prompt_budget(
        self,
        question: str,
        chunks: tuple[RetrievedChunk, ...],
        assessment: Any,
        state: RequestState,
    ) -> tuple[tuple[RetrievedChunk, ...], int]:
        """Drop evidence from the tail until the prompt fits the token budget.

        Why drop from the tail? The chunks are ranked; the least relevant
        passages are at the end. Dropping them is the cheapest way to obey the
        budget without changing the retrieval setting or calling a model.
        """
        remaining = self.settings.max_tokens_per_request - state.tokens_used
        max_output = min(self.settings.max_output_tokens, max(120, remaining // 3))
        working = list(chunks)
        while working:
            messages = build_generation_messages(
                question, tuple(working), assessment
            )
            estimated = estimate_messages_tokens(messages) + max_output
            if estimated <= remaining:
                return tuple(working), max_output
            if len(working) == 1:
                # One chunk still too large: reduce output headroom, then give
                # up rather than send an over-budget call.
                max_output = max(80, max_output - 40)
                if max_output <= 80:
                    return (), 0
                continue
            working.pop()
        return (), 0

    def _cache_key(self, normalized: NormalizedQuestion, preset: RetrievalPreset) -> str:
        return self.cache.make_key(
            normalized.normalized,
            preset=preset.name,
            model_id=self.provider.model_id,
            prompt_version=self.settings.prompt_version,
            corpus_version=self._corpus_versions[preset.name],
            temperature=self.settings.temperature,
            max_output_tokens=self.settings.max_output_tokens,
            source=self.provider.source,
        )

    def _select_preset(
        self,
        requested: str | None,
        *,
        allow_degradation: bool,
    ) -> tuple[RetrievalPreset, str | None]:
        """Choose a retrieval preset, optionally degrading under budget pressure."""
        name = requested or self.settings.retrieval_preset
        preset = self.retrieval_config.get(name)
        if not allow_degradation or not self.settings.degrade_under_pressure:
            return preset, None
        fraction = self.quota.daily_token_fraction_remaining()
        if fraction >= self.settings.budget_pressure_threshold:
            return preset, None
        # Degrade one tier: extensive -> balanced -> light.
        order = ["extensive", "balanced", "light"]
        index = order.index(preset.name) if preset.name in order else len(order) - 1
        degraded_name = order[min(index + 1, len(order) - 1)]
        degraded = self.retrieval_config.get(degraded_name)
        reason = (
            f"daily token budget {fraction:.0%} remaining is below "
            f"{self.settings.budget_pressure_threshold:.0%}; "
            f"degraded {preset.name} -> {degraded_name}"
        )
        return degraded, reason

    def _new_result(
        self,
        *,
        request_id: str,
        scenario: str,
        question: str,
        start_time: str,
        preset: RetrievalPreset,
        expected_grade: str | None,
    ) -> PipelineResult:
        return PipelineResult(
            request_id=request_id,
            scenario=scenario,
            source=self.provider.source,  # type: ignore[arg-type]
            question=question,
            start_time=start_time,
            first_token_ms=None,
            total_ms=0.0,
            status="ok",
            retries=0,
            tokens_in=0,
            tokens_out=0,
            retrieval_setting=preset.name,
            answer_grade=expected_grade,  # type: ignore[arg-type]
        )

    def _finalize(
        self,
        result: PipelineResult,
        state: RequestState,
        start_mono: float,
    ) -> None:
        """Copy measured counters into the result and close the timing."""
        result.total_ms = monotonic_ms() - start_mono
        result.tokens_in = state.tokens_in
        result.tokens_out = state.tokens_out
        result.model_calls = state.model_calls
        result.retries = state.retries
        result.first_token_ms = state.first_token_ms
        result.retrieval_ms = state.retrieval_ms
        result.generation_ms = state.generation_ms
        result.verification_ms = state.verification_ms
        result.retry_wait_ms = state.retry_wait_ms
        # ``queue_wait_ms`` is set by the caller from the scheduler result.
        # It stays None for requests that never entered the queue (cache hit,
        # scope refusal), which is the correct representation of "not measured".

    def _log_model_call(
        self,
        *,
        request_id: str,
        call_id: str,
        stage: str,
        attempt: int,
        response: ModelResponse | None,
        status: str,
        tokens_in: int,
        tokens_out: int,
        usage_estimated: bool,
        error_type: str | None,
        error_message: str | None,
        retry_after_s: float | None,
        preset_name: str,
        max_tokens: int,
        http_status: int | None = None,
    ) -> None:
        from the_relay.obs.traces import utc_now_iso

        record = ModelCallRecord(
            request_id=request_id,
            call_id=call_id,
            stage=stage,
            attempt=attempt,
            model_id=self.provider.model_id,
            source=self.provider.source,  # type: ignore[arg-type]
            started_at=response.started_at if response else utc_now_iso(),
            ended_at=response.ended_at if response else utc_now_iso(),
            duration_ms=response.total_ms if response else 0.0,
            first_token_ms=response.first_token_ms if response else None,
            status=status,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            total_tokens=tokens_in + tokens_out,
            usage_estimated=usage_estimated,
            http_status=http_status if http_status is not None else (response.http_status if response else None),
            error_type=error_type,
            error_message=error_message,
            retry_after_s=retry_after_s,
            rate_limit_headers=response.rate_limit_headers if response else {},
            config={
                "preset": preset_name,
                "max_tokens": max_tokens,
                "temperature": self.settings.temperature,
                "prompt_version": self.settings.prompt_version,
                "verification_mode": self.settings.verification_mode,
            },
        )
        # ``dataclasses.asdict`` keeps None values, which is what we want:
        # missing is null, never zero.
        from dataclasses import asdict

        self.model_call_log.write(asdict(record))

    async def aclose(self) -> None:
        await self.provider.aclose()
