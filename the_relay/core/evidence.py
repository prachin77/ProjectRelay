"""Stage H — deterministic evidence sufficiency screening.

WHY THIS STAGE EXISTS
---------------------
Retrieval always returns *something*. A similarity score is not proof that the
papers answer the question. If we send weak passages to the model and ask it to
answer, we are paying tokens to invite a hallucination. This stage decides:

* ``sufficient``   — the evidence covers the question; generate normally.
* ``partial``      — the evidence covers part of it; generate, but the answer
                     must state what is missing.
* ``insufficient`` — do not generate; tell the user the papers do not say.

HOW THE DECISION IS MADE
------------------------
All signals are local and deterministic:

1. **Query-term coverage** — what fraction of the question's content terms
   appear somewhere in the top passages?
2. **Top relevance** — the reranker's 0..1 score for the best passage.
3. **Supporting passage count** — how many passages clear a relevance floor.
4. **Numeric completeness** — if the question asks "how many / what score /
   what percentage", does the evidence actually contain a number? A passage
   about BLEU that contains no BLEU number cannot answer "what BLEU score?".
5. **Comparison breadth** — a comparison needs evidence about more than one
   item; one passage is usually a partial answer.

WHAT THIS CANNOT DO
-------------------
Lexical coverage is not entailment. The evidence can contain every word of the
question and still fail to answer it, or answer it with a different claim. That
is why generation is instructed to disclose gaps and why the verifier checks
claims against the exact passages. This stage is a *cheap filter*, not a truth
oracle. The evaluation set measures its false-refusal and false-accept rates.
"""

from __future__ import annotations

import re

from the_relay.config import Settings
from the_relay.core.scope import _content_tokens
from the_relay.rag.index import tokenize
from the_relay.types import EvidenceAssessment, RetrievedChunk

_NUMERIC_QUESTION_RE = re.compile(
    r"\b(?:how many|how much|what percentage|what fraction|what value|"
    r"what score|number of|how fast|how long|how large|what is the (?:bleu|wer|"
    r"latency|throughput)|bleu score|word error rate)\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)*\s*(?:%|k|m|bn|ms|s)?\b", re.IGNORECASE)
_COMPARISON_RE = re.compile(
    r"\b(?:compare|comparison|differ|difference|versus|vs\.?|better than|"
    r"worse than|contrast)\b",
    re.IGNORECASE,
)


def _coverage(query: str, chunks: tuple[RetrievedChunk, ...]) -> float:
    """Fraction of unique query content terms present in at least one chunk."""
    query_terms = set(_content_tokens(query))
    if not query_terms:
        return 0.0
    chunk_terms: set[str] = set()
    for chunk in chunks:
        chunk_terms.update(tokenize(chunk.text))
        # Also include title/section, because a question may name the paper
        # rather than a term in the body.
        chunk_terms.update(tokenize(chunk.title))
        if chunk.section:
            chunk_terms.update(tokenize(chunk.section))
    return len(query_terms & chunk_terms) / len(query_terms)


def assess_evidence(
    question: str,
    chunks: tuple[RetrievedChunk, ...],
    settings: Settings,
    *,
    absent_topic_terms: tuple[str, ...] = (),
    named_paper: str | None = None,
) -> EvidenceAssessment:
    """Return a deterministic sufficiency assessment.

    The thresholds are settings, not buried constants, because they must be
    tuned against the evaluation set and the tuning must be reproducible.

    ``absent_topic_terms`` is an *optional* corpus-level check computed by the
    caller when the question explicitly names a paper. It contains distinctive
    question terms that never occur in that paper's body (references excluded).
    If the named paper never mentions the topic, no amount of re-retrieval can
    find evidence, so the gate refuses immediately. This is what stops a
    topically adjacent question -- "which ImageNet learning rate?" -- from being
    answered with a passage that merely shares the word "learning rate".
    """
    if not chunks:
        return EvidenceAssessment(
            level="insufficient",
            reason="retrieval returned no passages",
            coverage=0.0,
            top1_score=0.0,
            supporting_chunks=0,
        )

    if absent_topic_terms:
        return EvidenceAssessment(
            level="insufficient",
            reason=(
                f"the question names the {named_paper or 'target'} paper, but its "
                "body never mentions: " + ", ".join(absent_topic_terms)
            ),
            coverage=_coverage(question, chunks),
            top1_score=chunks[0].relevance,
            supporting_chunks=sum(1 for chunk in chunks if chunk.relevance >= 0.20),
            documents=tuple(sorted({chunk.doc_id for chunk in chunks})),
        )

    coverage = _coverage(question, chunks)
    top1_score = chunks[0].relevance
    supporting_chunks = sum(1 for chunk in chunks if chunk.relevance >= 0.20)
    documents = tuple(sorted({chunk.doc_id for chunk in chunks}))

    numeric_requested = bool(_NUMERIC_QUESTION_RE.search(question))
    numeric_found = False
    if numeric_requested:
        # Look for any number in the top passages. We do not try to match the
        # exact requested unit here; that is too brittle. The verifier performs
        # the stricter number check on the generated claim.
        numeric_found = any(_NUMBER_RE.search(chunk.text) for chunk in chunks)

    comparison = bool(_COMPARISON_RE.search(question))

    # --- Decision rules ----------------------------------------------------
    # These are ordered from most to least permissive. The numbers are
    # provisional engineering thresholds (see PROJECT_JOURNAL ADR / config).
    if top1_score >= 0.45 and coverage >= settings.evidence_min_coverage:
        if numeric_requested and not numeric_found:
            return EvidenceAssessment(
                level="partial",
                reason=(
                    "the question asks for a number, but the retrieved passages "
                    "contain no number"
                ),
                coverage=coverage,
                top1_score=top1_score,
                supporting_chunks=supporting_chunks,
                documents=documents,
                numeric_requested=True,
                numeric_found=False,
            )
        if comparison and len(documents) < 2:
            return EvidenceAssessment(
                level="partial",
                reason=(
                    "the question asks for a comparison, but the evidence comes "
                    "from only one paper; the answer can only cover one side"
                ),
                coverage=coverage,
                top1_score=top1_score,
                supporting_chunks=supporting_chunks,
                documents=documents,
                numeric_requested=numeric_requested,
                numeric_found=numeric_found,
            )
        return EvidenceAssessment(
            level="sufficient",
            reason="strong top passage and good query-term coverage",
            coverage=coverage,
            top1_score=top1_score,
            supporting_chunks=supporting_chunks,
            documents=documents,
            numeric_requested=numeric_requested,
            numeric_found=numeric_found,
        )

    if top1_score >= settings.evidence_min_top1_score and coverage >= settings.evidence_partial_coverage:
        return EvidenceAssessment(
            level="partial",
            reason=(
                "relevant passages exist, but coverage is incomplete; the answer "
                "must state what the papers do not establish"
            ),
            coverage=coverage,
            top1_score=top1_score,
            supporting_chunks=supporting_chunks,
            documents=documents,
            numeric_requested=numeric_requested,
            numeric_found=numeric_found,
        )

    if supporting_chunks >= 2 and coverage >= settings.evidence_min_coverage:
        return EvidenceAssessment(
            level="partial",
            reason=(
                "several weakly relevant passages exist; the answer can only be "
                "partial"
            ),
            coverage=coverage,
            top1_score=top1_score,
            supporting_chunks=supporting_chunks,
            documents=documents,
            numeric_requested=numeric_requested,
            numeric_found=numeric_found,
        )

    return EvidenceAssessment(
        level="insufficient",
        reason=(
            "the retrieved passages do not cover enough of the question "
            f"(coverage {coverage:.2f}, top relevance {top1_score:.2f})"
        ),
        coverage=coverage,
        top1_score=top1_score,
        supporting_chunks=supporting_chunks,
        documents=documents,
        numeric_requested=numeric_requested,
        numeric_found=numeric_found,
    )


def refusal_message(assessment: EvidenceAssessment) -> str:
    """User-facing text when the papers do not contain enough evidence.

    It distinguishes "no evidence" from "partial evidence" so the user knows
    whether a different question might work. It never invents an answer.
    """
    if assessment.level == "insufficient":
        return (
            "The three papers in this library do not contain enough evidence to "
            "answer that reliably. I have not generated an answer, because doing "
            "so would mean guessing beyond the sources.\n\n"
            f"Retrieval detail: {assessment.reason}."
        )
    return (
        "The papers only partially support this question, and I could not verify "
        "a complete answer from the retrieved passages. I would rather say so "
        "than fill the gap with an unsupported claim."
    )
