"""A deterministic stand-in model service.

WHAT THIS IS
------------
A controllable imitation of the *interface and failure behaviour* of a hosted
chat model. It is not a language model. It exists so that the Relay's own
policies — queueing, deadlines, quota accounting, retries, streaming,
instrumentation — can be tested under load shapes the free tier cannot survive.

It implements the same ``ModelProvider`` interface as the real Groq adapter, so
the orchestrator does not know which backend it is talking to. Every measurement
it produces is labelled ``source="stand-in"`` and must never be merged with real
measurements without a label.

WHAT IT CAN SIMULATE
--------------------
* base latency + log-normal spread;
* time-to-first-token as a fraction of total time;
* piece-at-a-time streaming;
* request/token per-minute and per-day limits;
* 429-style refusal with ``retry-after``;
* 5xx-style temporary failures;
* timeouts;
* slowdown under concurrency;
* daily budget exhaustion;
* deterministic failure schedules for regression tests.

WHAT IT CANNOT SIMULATE
-----------------------
* real model computation or answer quality;
* the provider's hidden scheduling and load balancing;
* the real tokenizer;
* the content-dependent tail latency of a real generation;
* the exact distribution of real failures.

Those gaps are stated in the report. The purpose is to test the Relay's
policies, not to recreate Groq internally.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from the_relay.core.tokens import estimate_messages_tokens, estimate_tokens
from the_relay.core.verify import split_sentences
from the_relay.errors import (
    ProviderTimeoutError,
    RateLimitError,
    TransientProviderError,
)
from the_relay.providers.base import ModelProvider, TokenCallback
from the_relay.rag.index import content_terms
from the_relay.rag.targeting import detect_paper_target, stem_term
from the_relay.types import ModelRequest, ModelResponse, ModelUsage

_EVIDENCE_HEADER_RE = re.compile(r"^\[(S\d+)\] \((.*?)\)\s*$", re.MULTILINE)
_QUESTION_RE = re.compile(r"QUESTION:\s*(.*?)\s*EVIDENCE:", re.DOTALL)

# PDF extraction leaves artefacts that are technically sentences but carry no
# answer: figure captions, author/affiliation blocks, emails, running headers and
# reference entries. The stand-in is an extractive baseline, so it must at least
# refuse to quote these. Each pattern is narrow enough not to reject real prose.
_BOILERPLATE_PATTERNS = (
    # Only a caption that *starts* with "Figure 2" is boilerplate. A real
    # sentence such as "The big model (Table 2) reaches 28.4 BLEU" merely
    # refers to a table and is exactly the evidence we want. Rejecting every
    # mention of a table silently threw away the best answers.
    re.compile(r"^\s*(?:figure|fig\.|table|algorithm)\s*\d+\b", re.IGNORECASE),
    re.compile(r"^\s*\(?\s*(?:left|right|top|bottom|upper|lower|middle)\b", re.IGNORECASE),
    re.compile(r"\barxiv:\s*\d", re.IGNORECASE),
    re.compile(r"[\w.+-]+@[\w.-]+\.\w+"),
    re.compile(
        r"\b(?:university|institute|department|carnegie mellon|google brain|"
        r"google research|meta ai|kyutai|cartesia)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:et al\.|preprint|under review|copyright|all rights reserved|"
        r"equal contribution|listing order)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:designed, implemented|was responsible for|involved in|codebase|"
        r"tensor2tensor|countless model variants|visuali[sz]ations)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:abstract|introduction|related work|conclusion|references|appendix)\b",
        re.IGNORECASE,
    ),
)

# PDF extraction often glues a short heading to the sentence that follows:
# "Decoder: The decoder is also composed...". The label is metadata, not part of
# the claim, so it is removed before scoring and before citing.
_LEADING_LABEL_RE = re.compile(
    r"^\s*(?:encoder|decoder|routing module|downsampler|upsampler|"
    r"smoothing module|chunking layer|dechunking layer|"
    r"temporal transformer|depth transformer)\s*:\s*",
    re.IGNORECASE,
)

_DEFINITION_RE = re.compile(
    r"\b(?:consists? of|composed of|defined as|refers? to|means|we use|"
    r"we propose|allows?|enables?|is responsible for|performs?|"
    r"we introduce|is a|are a)\b",
    re.IGNORECASE,
)
_PROPER_NOUN_RUN_RE = re.compile(
    r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){2,})\b"
)
_SKIP_SOURCE_SECTION_RE = re.compile(
    r"§\s*(?:references|bibliography|acknowledg)", re.IGNORECASE
)
_NUMERIC_TOKEN_RE = re.compile(r"^[\d.,×xX%()/+-]+$")


def _looks_like_table_row(sentence: str) -> bool:
    """Detect a row of a results table that PDF extraction turned into prose.

    A sentence such as ``N d model dff h d k dv Pdrop ... 6 512 2048 8 64 64``
    is not an answer, but it matches many query words and used to crowd out real
    sentences. A high proportion of numeric/single-character tokens is a cheap
    and reliable signal; real prose almost never looks like that.
    """
    tokens = sentence.split()
    if len(tokens) < 8:
        return False
    numeric_or_symbol = sum(
        1
        for token in tokens
        if _NUMERIC_TOKEN_RE.match(token) or len(token.strip("(),.")) <= 1
    )
    if numeric_or_symbol / len(tokens) >= 0.45:
        return True
    # Several dense numeric runs on one "line" is a table, not a sentence.
    if len(re.findall(r"\b\d+(?:\.\d+)?\b", sentence)) >= 12:
        return True
    return False

# Terms that identify which paper a question is about. The shared targeting
# module is the single source of truth, so retrieval and the stand-in cannot
# disagree about which paper a question names.
def _target_doc(question: str) -> str | None:
    """Return the paper a question names, if exactly one is named."""
    target = detect_paper_target(question)
    return target.doc_id if target is not None else None


def _doc_matches(label: str, target_doc: str) -> bool:
    lowered = label.lower()
    marker = {
        "moshi": "moshi",
        "attention": "attention",
        "dynamic_chunking": "dynamic",
    }[target_doc]
    return marker in lowered


def _strip_section_heading(sentence: str, label: str) -> str:
    """Remove a leading ``5.3 Optimizer`` copied from the page layout.

    WHY NOT A GREEDY REGEX?
    -----------------------
    The first version used ``^\\d+... [A-Z][A-Za-z0-9 ,-]{2,60}\\s+``. On
    ``5.3 Optimizer We used the Adam optimizer [20] ...`` it consumed the
    heading *and* the real subject of the sentence, leaving ``[20] with ...``.
    That silently deleted the answer. Here we use the known section name from
    the citation label, so only the actual heading can be removed.
    """
    match = re.search(r"§\s*(.+)$", label)
    if match:
        section = re.sub(r"\s+", " ", match.group(1)).strip()
        if section and sentence.startswith(section):
            return sentence[len(section):].lstrip(" .:-")
    # Fall back to removing only a numeric heading prefix like "3.2.1"; do not
    # guess where the title ends.
    return re.sub(r"^\d+(?:\.\d+)*\s+", "", sentence)


def _is_boilerplate(sentence: str) -> bool:
    """Return True for extraction artefacts that should never be quoted."""
    if any(pattern.search(sentence) for pattern in _BOILERPLATE_PATTERNS):
        return True
    # Three or more consecutive capitalised words is almost always an author
    # list, a title line, or a running header, not an explanatory sentence.
    if _PROPER_NOUN_RUN_RE.search(sentence):
        return True
    # A line with no terminal punctuation and few words is a heading.
    words = sentence.split()
    if len(words) < 12 and not sentence.rstrip().endswith((".", "!", "?", ":")):
        return True
    if _looks_like_table_row(sentence):
        return True
    return False


@dataclass
class StandInConfig:
    """Every knob of the simulator. Loaded from JSON, overridable in tests."""

    base_latency_ms: float = 850.0
    latency_sigma: float = 0.30
    ttft_fraction: float = 0.35
    stream_chunk_ms: float = 20.0
    failure_rate: float = 0.02
    failure_kinds: tuple[str, ...] = ("server_error",)
    timeout_probability: float = 0.01
    timeout_after_ms: float = 18_000.0
    max_concurrency: int = 8
    load_slowdown_per_call: float = 0.12
    queue_slope_ms: float = 80.0
    rpm: int = 30
    tpm: int = 8_000
    rpd: int = 1_000
    tpd: int = 200_000
    day_window_s: float = 86_400.0
    retry_after_s: float = 2.0
    seed: int = 42
    fail_on_calls: tuple[int, ...] = ()
    rate_limit_on_calls: tuple[int, ...] = ()
    timeout_on_calls: tuple[int, ...] = ()
    # If set, the daily budget is lower than tpd/rpd. Used by the
    # "daily quota exhausted" scenario.
    daily_budget_tokens: int | None = None
    daily_budget_requests: int | None = None
    note: str = "provisional engineering settings, not calibrated to Groq"

    @classmethod
    def from_file(cls, path: Path, **overrides: Any) -> "StandInConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        # Ignore documentation-only keys.
        data.pop("note", None)
        for key in ("failure_kinds", "fail_on_calls", "rate_limit_on_calls", "timeout_on_calls"):
            if key in data and data[key] is not None:
                data[key] = tuple(data[key])
        known = {field: data[field] for field in cls.__dataclass_fields__ if field in data}
        known.update(overrides)
        return cls(**known)

    def copy_with(self, **overrides: Any) -> "StandInConfig":
        data = {field: getattr(self, field) for field in self.__dataclass_fields__}
        data.update(overrides)
        return StandInConfig(**data)


class StandInProvider(ModelProvider):
    """Seeded, quota-aware, streaming stand-in."""

    source = "stand-in"

    def __init__(
        self,
        config: StandInConfig,
        *,
        model_id: str = "standin/deterministic-extractive-v1",
    ) -> None:
        self.config = config
        self.model_id = model_id
        self._rng = random.Random(config.seed)
        self._lock = asyncio.Lock()
        self._request_times: deque[float] = deque()
        self._token_events: deque[list[float]] = deque()  # [timestamp, tokens]
        self._in_flight = 0
        self._call_index = 0
        self._closed = False

    # ------------------------------------------------------------------
    # Quota simulation
    # ------------------------------------------------------------------
    def _prune(self, now: float) -> None:
        window_start = now - self.config.day_window_s
        while self._request_times and self._request_times[0] < window_start:
            self._request_times.popleft()
        while self._token_events and self._token_events[0][0] < window_start:
            self._token_events.popleft()

    def _minute_requests(self, now: float) -> int:
        return sum(1 for t in self._request_times if t >= now - 60.0)

    def _minute_tokens(self, now: float) -> int:
        return int(sum(tokens for t, tokens in self._token_events if t >= now - 60.0))

    def _day_requests(self) -> int:
        return len(self._request_times)

    def _day_tokens(self) -> int:
        return int(sum(tokens for _t, tokens in self._token_events))

    async def _check_and_reserve_quota(self, estimated_tokens: int) -> list[float]:
        """Check simulated limits and reserve an estimated token budget.

        Returns the token event whose value will be reconciled after the call.
        The reservation is deliberately at the *estimated input + max output*
        level: that is what a real provider counts against TPM when the request
        is accepted.
        """
        async with self._lock:
            now = time.monotonic()
            self._prune(now)
            rpm_limit = self.config.rpm
            tpm_limit = self.config.tpm
            rpd_limit = self.config.daily_budget_requests or self.config.rpd
            tpd_limit = self.config.daily_budget_tokens or self.config.tpd

            if self._minute_requests(now) + 1 > rpm_limit:
                oldest = min(
                    (t for t in self._request_times if t >= now - 60.0),
                    default=now,
                )
                raise RateLimitError(
                    f"stand-in simulated RPM limit ({rpm_limit}/min) reached",
                    retry_after_s=max(0.1, 60.0 - (now - oldest)),
                    kind="rate_limit_rpm",
                )
            if self._day_requests() + 1 > rpd_limit:
                raise RateLimitError(
                    f"stand-in simulated RPD limit ({rpd_limit}/day) reached",
                    retry_after_s=self.config.day_window_s,
                    kind="daily_quota_rpd",
                )
            if self._minute_tokens(now) + estimated_tokens > tpm_limit:
                oldest = min(
                    (t for t, _ in self._token_events if t >= now - 60.0),
                    default=now,
                )
                raise RateLimitError(
                    f"stand-in simulated TPM limit ({tpm_limit}/min) reached",
                    retry_after_s=max(0.1, 60.0 - (now - oldest)),
                    kind="rate_limit_tpm",
                )
            if self._day_tokens() + estimated_tokens > tpd_limit:
                raise RateLimitError(
                    f"stand-in simulated TPD limit ({tpd_limit}/day) reached",
                    retry_after_s=self.config.day_window_s,
                    kind="daily_quota_tpd",
                )

            event = [now, float(estimated_tokens)]
            self._request_times.append(now)
            self._token_events.append(event)
            return event

    async def preconsume_daily_budget(
        self,
        *,
        tokens: int | None = None,
        requests: int | None = None,
    ) -> None:
        """Fill the simulated daily budget for the exhaustion scenario.

        This is a *test control*, not a normal API. It makes the next real
        request hit a simulated daily limit without spending thousands of
        simulated calls first.
        """
        async with self._lock:
            now = time.monotonic()
            token_amount = tokens if tokens is not None else self.config.tpd
            request_amount = requests if requests is not None else self.config.rpd
            self._token_events.append([now, float(token_amount)])
            for _ in range(max(1, request_amount)):
                self._request_times.append(now)

    def quota_snapshot(self) -> dict[str, Any]:
        """Current simulated usage, for traces and tests."""
        now = time.monotonic()
        return {
            "rpm_used": self._minute_requests(now),
            "rpm_limit": self.config.rpm,
            "tpm_used": self._minute_tokens(now),
            "tpm_limit": self.config.tpm,
            "rpd_used": self._day_requests(),
            "rpd_limit": self.config.daily_budget_requests or self.config.rpd,
            "tpd_used": self._day_tokens(),
            "tpd_limit": self.config.daily_budget_tokens or self.config.tpd,
            "in_flight": self._in_flight,
        }

    # ------------------------------------------------------------------
    # Main interface
    # ------------------------------------------------------------------
    async def generate(
        self,
        request: ModelRequest,
        on_token: TokenCallback | None = None,
    ) -> ModelResponse:
        """Simulate one completion.

        Order of operations matters for testing:
        1. quota check (may raise a 429-style error);
        2. deterministic failure schedule (for regression tests);
        3. random failure / timeout;
        4. latency and streaming;
        5. usage reconciliation.
        """
        started = time.monotonic()
        started_at = _utc_iso()
        input_tokens = estimate_messages_tokens(request.messages)
        estimated_tokens = input_tokens + request.max_tokens

        async with self._lock:
            self._call_index += 1
            call_index = self._call_index

        # 1. Quota. This is the stand-in's *own* provider-side limit; the
        # application quota manager is a separate layer and both are visible in
        # traces. That separation is intentional: it lets us test what happens
        # when the provider refuses even though our local estimate said yes.
        event = await self._check_and_reserve_quota(estimated_tokens)

        # 2. Deterministic failure schedule.
        if call_index in self.config.rate_limit_on_calls:
            raise RateLimitError(
                "stand-in deterministic rate-limit schedule",
                retry_after_s=self.config.retry_after_s,
                kind="rate_limit_scheduled",
            )
        if call_index in self.config.timeout_on_calls:
            raise ProviderTimeoutError("stand-in deterministic timeout schedule")
        if call_index in self.config.fail_on_calls:
            raise TransientProviderError("stand-in deterministic server failure", http_status=503)

        # 3. Random failures. The seed makes a run reproducible.
        if self._rng.random() < self.config.timeout_probability:
            raise ProviderTimeoutError("stand-in simulated timeout")
        if self._rng.random() < self.config.failure_rate:
            kind = self._rng.choice(self.config.failure_kinds)
            if kind == "timeout":
                raise ProviderTimeoutError("stand-in simulated timeout")
            raise TransientProviderError(
                f"stand-in simulated {kind}", http_status=503
            )

        # 4. Latency. Log-normal looks more like real tail latency than a
        # uniform distribution, and it is the shape we would fit from a real
        # probe if one existed.
        async with self._lock:
            self._in_flight += 1
            in_flight = self._in_flight

        try:
            multiplier = math.exp(
                self._rng.normalvariate(0.0, max(0.01, self.config.latency_sigma))
            )
            load_slowdown = 1.0 + self.config.load_slowdown_per_call * max(0, in_flight - 1)
            queue_penalty = self.config.queue_slope_ms * max(
                0, in_flight - self.config.max_concurrency
            )
            total_ms = max(5.0, self.config.base_latency_ms * multiplier * load_slowdown + queue_penalty)

            if self.config.timeout_after_ms and total_ms > self.config.timeout_after_ms:
                await asyncio.sleep(self.config.timeout_after_ms / 1000.0)
                raise ProviderTimeoutError("stand-in simulated timeout (latency exceeded)")

            answer = self._build_answer(request)
            # Keep the simulated output within the requested output budget.
            while estimate_tokens(answer) > request.max_tokens and len(answer) > 40:
                sentences = split_sentences(answer)
                if len(sentences) <= 1:
                    answer = answer[: int(request.max_tokens * 3.5)]
                    break
                answer = " ".join(sentences[:-1])

            first_token_ms: float | None = None
            streaming = on_token is not None
            if streaming:
                ttft_ms = max(1.0, total_ms * min(1.0, max(0.0, self.config.ttft_fraction)))
                await asyncio.sleep(ttft_ms / 1000.0)
                first_token_ms = (time.monotonic() - started) * 1000.0
                words = answer.split(" ")
                # Send a few words at a time, like a real stream.
                pieces = [" ".join(words[i:i + 3]) for i in range(0, len(words), 3)] or [answer]
                remaining_ms = max(1.0, total_ms - ttft_ms)
                per_piece = max(0.0, (remaining_ms - self.config.stream_chunk_ms) / max(1, len(pieces)))
                for index, piece in enumerate(pieces):
                    text_piece = piece if index == 0 else " " + piece
                    await _emit(on_token, text_piece, (time.monotonic() - started) * 1000.0)
                    await asyncio.sleep((per_piece + self.config.stream_chunk_ms) / 1000.0)
            else:
                await asyncio.sleep(total_ms / 1000.0)

            ended = time.monotonic()
            output_tokens = estimate_tokens(answer)
            usage = ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                estimated=True,
            )
            # Reconcile the reservation with actual simulated usage.
            async with self._lock:
                event[1] = float(usage.total_tokens)

            return ModelResponse(
                text=answer,
                model_id=self.model_id,
                source="stand-in",
                usage=usage,
                first_token_ms=first_token_ms,
                total_ms=(ended - started) * 1000.0,
                http_status=200,
                finish_reason="stop",
                rate_limit_headers=self._quota_headers(),
                started_at=started_at,
                ended_at=_utc_iso(),
            )
        finally:
            async with self._lock:
                self._in_flight = max(0, self._in_flight - 1)

    async def aclose(self) -> None:
        self._closed = True

    # ------------------------------------------------------------------
    # Answer generation (deterministic, extractive, not an LLM)
    # ------------------------------------------------------------------
    def _build_answer(self, request: ModelRequest) -> str:
        """Build a deterministic extractive answer from the evidence block.

        This is deliberately not intelligent. It selects sentences that contain
        the question's content terms and attaches the right source id. Its job
        is to exercise the pipeline and produce checkable citations, not to
        emulate model reasoning.
        """
        user_text = "\n".join(
            str(message.get("content", ""))
            for message in request.messages
            if message.get("role") == "user"
        )

        if request.task == "verify":
            # A naive verifier: it accepts an answer that contains at least one
            # citation and no explicit "unsupported" marker. This is enough to
            # test that the optional LLM-verification code path is wired.
            has_citation = bool(re.search(r"\[S\d+", user_text))
            return json.dumps(
                {
                    "supported": has_citation,
                    "unsupported_claims": [] if has_citation else ["no citation found"],
                    "invalid_citations": [],
                }
            )

        question_match = _QUESTION_RE.search(user_text)
        question = question_match.group(1).strip() if question_match else ""

        # Split the evidence block into (source_id, label, text) sections.
        matches = list(_EVIDENCE_HEADER_RE.finditer(user_text))
        sources: list[tuple[str, str, str]] = []
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(user_text)
            body = user_text[start:end]
            body = re.split(r"\nINSTRUCTION:", body, maxsplit=1)[0]
            sources.append((match.group(1), match.group(2), body.strip()))

        if not sources:
            return (
                "The provided evidence does not contain enough information to "
                "answer this question, so I will not guess."
            )

        target_doc = _target_doc(question)
        query_terms_list = content_terms(question)
        query_terms = set(query_terms_list)

        # --- Collect candidate sentences ---------------------------------
        raw: list[tuple[str, str, str, int]] = []
        for source_id, label, body in sources:
            if _SKIP_SOURCE_SECTION_RE.search(label):
                continue
            # When the question names one paper, never quote another paper.
            # Retrieval already filters, but the stand-in must be safe if it is
            # called directly with a mixed evidence block (as tests do).
            if target_doc and not _doc_matches(label, target_doc):
                continue
            kept_position = 0
            for sentence in split_sentences(body):
                sentence = _LEADING_LABEL_RE.sub("", sentence.strip())
                if len(sentence.split()) < 4:
                    continue
                if _is_boilerplate(sentence):
                    continue
                # Remove a leading section number/title if it dominates the
                # sentence; the citation label already carries the section.
                sentence = _strip_section_heading(sentence, label)
                if not content_terms(sentence):
                    continue
                raw.append((source_id, label, sentence, kept_position))
                kept_position += 1

        if not raw:
            return (
                "The provided evidence does not contain enough information to "
                "answer this question, so I will not guess."
            )

        # --- Weight query terms by how rare they are in the evidence ------
        # "warmup" or "dechunking" identifies the answer sentence; "model" and
        # "paper" do not. A sentence-frequency weight is a cheap, deterministic
        # stand-in for IDF and needs no corpus-wide statistics.
        sentence_frequency: dict[str, int] = {}
        for _sid, _label, sentence, _pos in raw:
            for term in set(content_terms(sentence)):
                sentence_frequency[term] = sentence_frequency.get(term, 0) + 1
        weights = {
            term: 1.0 / (1.0 + sentence_frequency.get(term, 0))
            for term in query_terms
        }
        total_weight = sum(weights.values()) or 1.0

        query_bigrams = set(zip(query_terms_list, query_terms_list[1:]))
        asks_for_number = bool(
            re.search(
                r"\b(?:how many|how much|what score|what percentage|what latency|"
                r"how large|how long|what bleu|what wer)\b",
                question,
                re.IGNORECASE,
            )
        )
        query_numbers = set(re.findall(r"\d+(?:\.\d+)?", question))

        candidates: list[tuple[float, str, str]] = []
        for source_id, label, sentence, position in raw:
            terms = set(content_terms(sentence))
            term_stems = {stem_term(term) for term in terms}
            matched_weight = sum(
                weight
                for term, weight in weights.items()
                if term in terms or stem_term(term) in term_stems
            )
            coverage = matched_weight / total_weight

            sentence_tokens = content_terms(sentence)
            sentence_bigrams = set(zip(sentence_tokens, sentence_tokens[1:]))
            bigram = (
                len(query_bigrams & sentence_bigrams) / len(query_bigrams)
                if query_bigrams
                else 0.0
            )

            score = 0.75 * coverage + 0.15 * bigram
            if target_doc and _doc_matches(label, target_doc):
                score += 0.15
            # The section a sentence comes from is a strong signal. A sentence
            # under "2.2.2 Dechunking" is more likely to explain dechunking than
            # a passing mention under "1 Introduction", even when the passing
            # mention repeats more query words.
            label_terms = set(content_terms(label))
            if query_terms:
                score += 0.12 * (
                    sum(1 for term in query_terms if term in label_terms)
                    / len(query_terms)
                )
            if _DEFINITION_RE.search(sentence):
                score += 0.05
            sentence_numbers = set(re.findall(r"\d+(?:\.\d+)?", sentence))
            if asks_for_number and sentence_numbers:
                # A numeric question ("how many / how long / what latency")
                # is answered by a sentence with a number. Without this, a
                # generic sentence that happens to repeat more query words wins
                # over "Training took 3.5 days on 8 P100 GPUs."
                score += 0.18
            if query_numbers and query_numbers & sentence_numbers:
                score += 0.10
            if position == 0:
                # The first sentence after a section heading is usually the
                # definition or the key claim. It is worth one extra slot even
                # when it repeats fewer query words than a later sentence.
                score += 0.10
            candidates.append((score, source_id, sentence))

        candidates.sort(key=lambda row: row[0], reverse=True)
        selected: list[tuple[str, str]] = []
        seen_sentences: set[str] = set()
        per_source: dict[str, int] = {}
        for score, source_id, sentence in candidates:
            if score < 0.18:
                continue
            key = sentence.lower()[:80]
            if key in seen_sentences:
                continue
            # At most two sentences from one passage. A single chunk often holds
            # both halves of an answer (for example the optimizer and the warmup
            # count), but four sentences from one passage is a quote, not an
            # answer.
            if per_source.get(source_id, 0) >= 2:
                continue
            selected.append((source_id, sentence))
            seen_sentences.add(key)
            per_source[source_id] = per_source.get(source_id, 0) + 1
            if len(selected) >= 4:
                break

        if not selected:
            return (
                "The provided evidence does not contain enough information to "
                "answer this question, so I will not guess."
            )

        pieces: list[str] = []
        for source_id, sentence in selected:
            sentence = sentence.strip()
            if not sentence.endswith((".", "!", "?")):
                sentence += "."
            pieces.append(f"{sentence} [{source_id}]")
        return " ".join(pieces)

    def _quota_headers(self) -> dict[str, str]:
        """Mimic the header names documented by Groq.

        A real provider returns these on every response; the stand-in does the
        same so the instrumentation path is exercised. The semantics follow the
        documentation: ``limit-requests`` is RPD and ``limit-tokens`` is TPM.
        """
        snapshot = self.quota_snapshot()
        return {
            "x-ratelimit-limit-requests": str(snapshot["rpd_limit"]),
            "x-ratelimit-remaining-requests": str(
                max(0, snapshot["rpd_limit"] - snapshot["rpd_used"])
            ),
            "x-ratelimit-limit-tokens": str(snapshot["tpm_limit"]),
            "x-ratelimit-remaining-tokens": str(
                max(0, snapshot["tpm_limit"] - snapshot["tpm_used"])
            ),
            "x-ratelimit-reset-requests": "not applicable (stand-in rolling window)",
            "x-ratelimit-reset-tokens": "60s",
        }


async def _emit(callback: TokenCallback | None, text: str, elapsed_ms: float) -> None:
    if callback is None:
        return
    result = callback(text, elapsed_ms)
    if asyncio.iscoroutine(result):
        await result


def _utc_iso() -> str:
    from the_relay.obs.traces import utc_now_iso

    return utc_now_iso()
