"""Stage J — citation and grounding verification.

THE QUALITY PROBLEM
-------------------
A citation is not a decoration. "The Transformer uses multi-head attention
[S1]" is only acceptable if:

1. ``S1`` was actually retrieved for this question;
2. the cited passage exists in the library;
3. the passage supports the claim, not merely the paper's topic;
4. numbers in the claim appear in the passage;
5. every factual sentence has a citation, not just the first one.

This module checks all five deterministically. It is intentionally conservative:
if support is unclear, it fails the answer and the pipeline either regenerates
once or returns a transparent partial answer.

WHAT THIS IS NOT
----------------
It is not an entailment model. Lexical overlap can be fooled by a passage that
mentions the right words but makes a different claim. That limitation is real
and is stated in the report. The optional LLM verifier exists to test whether a
second model call improves this, but it is off by default because it roughly
doubles token cost on an 8k-TPM budget.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from the_relay.core.scope import _content_tokens
from the_relay.rag.index import tokenize
from the_relay.types import RetrievedChunk, VerificationResult

_CITATION_RE = re.compile(r"\[(S\d+(?:\s*,\s*S\d+)*)\]")
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)*\s*(?:%|k|m|bn|ms|s)?\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=\S)")

# Abbreviations whose trailing full stop is not a sentence boundary. Without
# this, "model deployment (e.g. on a laptop)" becomes two fragments, and the
# second fragment inherits the citation intended for the whole sentence.
_ABBREVIATION_RE = re.compile(
    r"\b(?:e\.g|i\.e|et al|Fig|Eq|vs|Dr|Mr|Ms|Mrs|cf|approx|Sec|Tab|No|"
    r"al|resp|etc)\.(?=\s)",
    re.IGNORECASE,
)
_ABBREVIATION_PLACEHOLDER = "\u0000DOT\u0000"

# Sentences that describe the limits of the evidence rather than asserting a
# fact about the papers. They do not need a citation, but any citations they
# contain must still exist.
_META_PATTERNS = (
    re.compile(r"\bthe papers? (?:do|does) not\b", re.IGNORECASE),
    re.compile(r"\bnot (?:enough|sufficient) evidence\b", re.IGNORECASE),
    re.compile(r"\b(?:i )?(?:cannot|can't|am unable to)\b", re.IGNORECASE),
    re.compile(r"\bno evidence\b", re.IGNORECASE),
    re.compile(r"\bdo not establish\b", re.IGNORECASE),
    re.compile(r"\bnot (?:provided|reported|described|specified)\b", re.IGNORECASE),
    re.compile(r"\bthe provided evidence\b", re.IGNORECASE),
    re.compile(r"\bonly partially\b", re.IGNORECASE),
    re.compile(r"\bI (?:do not|don't) know\b", re.IGNORECASE),
)

# Very common words that should not count towards lexical support. They make
# coverage look good without carrying a claim.
_SUPPORT_STOPWORDS = frozenset(
    """
    paper papers authors author study work also however therefore thus this that
    these those it its they their there here which who whom what when where why
    how can could may might should would will shall do does did is are was were
    be been being have has had not no yes and or but if then than of in on at to
    for from by with without into about over under between among during before
    after above below up down out off again further once only own same so too
    very just more most other some such any both each few all one two three
    """.split()
)


def _protect_abbreviations(text: str) -> str:
    """Temporarily hide abbreviation full stops from the sentence splitter.

    ``e.g. on a laptop`` must not split into ``e.g.`` + ``on a laptop``. We
    replace only the *final* full stop of each abbreviation with a placeholder,
    leaving the abbreviation's internal dots intact, then restore it after
    splitting. Restoring the whole match would turn ``e.g.`` into ``e.``.
    """
    def repl(match: re.Match[str]) -> str:
        matched = match.group(0)
        return matched[:-1] + _ABBREVIATION_PLACEHOLDER

    return _ABBREVIATION_RE.sub(repl, text)


def normalise_citation_brackets(text: str) -> str:
    """Turn full-width / CJK citation brackets into ASCII ``[ ]``.

    A real model occasionally emits ``【S1】`` (U+3010/U+3011) because those
    characters are common in its training data. That is a formatting variant,
    not a wrong citation, and treating it as "uncited" produced a false refusal
    in a live Groq run. Normalising here keeps the verifier strict about the
    *id* while being agnostic about the bracket glyph.
    """
    if not text:
        return text
    return (
        text.replace("\u3010", "[")
        .replace("\u3011", "]")
        .replace("\uff3b", "[")
        .replace("\uff3d", "]")
    )


def split_sentences(text: str) -> list[str]:
    """Split an answer into claim-sized sentences.

    Markdown bullets and newlines are treated as sentence boundaries. This is a
    heuristic, but it is the unit a human reviewer would check: one sentence,
    one claim, one citation.

    Citation placement needs two repairs after naive punctuation splitting:

    * ``"... attention. [S1]"`` leaves ``[S1]`` as a fragment. A citation-only
      fragment is appended to the sentence before it.
    * ``"A. [S1] B. [S2]"`` leaves ``"[S1] B."`` as a fragment. A leading
      citation is moved back to the sentence before it, and the remainder starts
      a new sentence. Without this, B would inherit A's citation.
    """
    cleaned = re.sub(r"^\s*[-*•]\s*", "", text, flags=re.MULTILINE)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = _protect_abbreviations(cleaned)

    parts: list[str] = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            sentence = sentence.strip()
            if sentence:
                parts.append(sentence.replace(_ABBREVIATION_PLACEHOLDER, "."))

    citation_prefix = re.compile(
        r"^((?:\[S\d+(?:\s*,\s*S\d+)*\]\s*)+)(.*)$", re.DOTALL
    )
    citation_only = re.compile(r"^\[S\d+(?:\s*,\s*S\d+)*\]$")
    merged: list[str] = []
    for part in parts:
        prefix_match = citation_prefix.match(part)
        if prefix_match and merged:
            leading = prefix_match.group(1).strip()
            rest = prefix_match.group(2).strip()
            merged[-1] = f"{merged[-1]} {leading}"
            if rest:
                merged.append(rest)
        elif citation_only.match(part) and merged:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


def extract_citation_ids(sentence: str) -> list[str]:
    """Return every ``S#`` mentioned in a sentence, in order, deduplicated."""
    ids: list[str] = []
    for match in _CITATION_RE.finditer(sentence):
        for part in match.group(1).split(","):
            source_id = part.strip()
            if source_id and source_id not in ids:
                ids.append(source_id)
    return ids


def _is_meta_sentence(sentence: str) -> bool:
    return any(pattern.search(sentence) for pattern in _META_PATTERNS)


def _strip_citations(sentence: str) -> str:
    """Remove ``[S#]`` markers before checking lexical support.

    Citation ids are metadata, not claim content. If they are left in, the
    tokenizer turns ``[S3]`` into ``s3`` and that invented term is counted as
    unmatched, which unfairly lowers the overlap score.
    """
    return _CITATION_RE.sub(" ", sentence)


def _claim_terms(sentence: str) -> list[str]:
    terms = [
        term
        for term in _content_tokens(_strip_citations(sentence))
        if term not in _SUPPORT_STOPWORDS and len(term) > 2
    ]
    return terms


def _numbers(text: str) -> set[str]:
    return {
        re.sub(r"\s+", "", m.group(0).strip().lower())
        for m in _NUMBER_RE.finditer(text)
    }


def claim_is_supported_by_chunk(claim: str, chunk: RetrievedChunk) -> tuple[bool, float]:
    """Check whether one passage supports one claim.

    Returns ``(supported, overlap)``. The overlap is the fraction of the claim's
    content terms present in the passage. It is a lexical proxy, not proof of
    entailment; the thresholds are deliberately conservative.

    Special cases:
    - If the claim contains an exact long substring of the passage, accept.
    - Every number in the claim must appear in the passage. A passage about
      BLEU that says "28.4" supports a claim of 28.4, not 41.8.
    """
    claim_for_check = _strip_citations(claim)
    claim_numbers = _numbers(claim_for_check)
    chunk_numbers = _numbers(chunk.text)
    if claim_numbers and not claim_numbers <= chunk_numbers:
        return False, 0.0

    normalized_claim = re.sub(r"\s+", " ", claim_for_check.lower()).strip(" .")
    normalized_chunk = re.sub(r"\s+", " ", chunk.text.lower())
    if len(normalized_claim) >= 40 and normalized_claim in normalized_chunk:
        return True, 1.0

    terms = _claim_terms(claim)
    if not terms:
        # A sentence with no content terms is not a factual claim we can check;
        # treat it as unsupported rather than silently passing it.
        return False, 0.0

    chunk_terms = set(tokenize(chunk.text))
    matched = sum(1 for term in terms if term in chunk_terms)
    overlap = matched / len(terms)

    # Short claims need stronger overlap, because one matching word can be a
    # coincidence. Longer claims converge to the 0.35 threshold.
    threshold = 0.50 if len(terms) <= 5 else 0.35
    return overlap >= threshold, overlap


def verify_answer(
    answer: str,
    chunks: tuple[RetrievedChunk, ...],
    *,
    method: str = "deterministic",
) -> VerificationResult:
    """Check an answer against the passages that were actually retrieved.

    The function never calls a model. It returns a structured result so the
    pipeline can log exactly why an answer failed and so a partial-answer
    fallback can keep the supported sentences.
    """
    if not answer or not answer.strip():
        return VerificationResult(
            passed=False,
            issues=("the answer is empty",),
            invalid_citations=(),
            unsupported_claims=(),
            claim_count=0,
            supported_claim_count=0,
            method=method,
        )

    answer = normalise_citation_brackets(answer)
    by_id = {chunk.source_id: chunk for chunk in chunks if chunk.source_id}
    sentences = split_sentences(answer)
    issues: list[str] = []
    invalid_citations: list[str] = []
    unsupported_claims: list[str] = []
    claim_count = 0
    supported_count = 0

    for sentence in sentences:
        citation_ids = extract_citation_ids(sentence)
        # A citation id must exist in the retrieved set, even in a meta
        # sentence. A made-up id is always an error.
        for source_id in citation_ids:
            if source_id not in by_id:
                invalid_citations.append(source_id)
                issues.append(
                    f"citation {source_id} was not in the retrieved evidence"
                )

        if _is_meta_sentence(sentence):
            continue

        claim_count += 1
        if not citation_ids:
            unsupported_claims.append(sentence)
            issues.append(f"claim has no citation: {sentence[:90]!r}")
            continue

        valid_citations = [cid for cid in citation_ids if cid in by_id]
        if not valid_citations:
            unsupported_claims.append(sentence)
            continue

        supported = False
        best_overlap = 0.0
        for source_id in valid_citations:
            ok, overlap = claim_is_supported_by_chunk(sentence, by_id[source_id])
            best_overlap = max(best_overlap, overlap)
            if ok:
                supported = True
                break
        if supported:
            supported_count += 1
        else:
            unsupported_claims.append(sentence)
            issues.append(
                "cited passage does not support the claim "
                f"(best lexical overlap {best_overlap:.2f}): {sentence[:90]!r}"
            )

    # A pure refusal has zero factual claims. It can pass if it is not empty and
    # does not contain an invalid citation. That is the correct behaviour for an
    # insufficient-evidence question.
    if claim_count == 0 and invalid_citations:
        issues.append("the refusal contains an invalid citation")

    passed = (
        not invalid_citations
        and not unsupported_claims
        and (claim_count > 0 or bool(sentences))
    )
    return VerificationResult(
        passed=passed,
        issues=tuple(dict.fromkeys(issues)),
        invalid_citations=tuple(dict.fromkeys(invalid_citations)),
        unsupported_claims=tuple(unsupported_claims),
        claim_count=claim_count,
        supported_claim_count=supported_count,
        method=method,
    )


def supported_sentences(
    answer: str,
    chunks: tuple[RetrievedChunk, ...],
) -> list[str]:
    """Return only the sentences that pass the deterministic support check.

    Used for the transparent partial-answer fallback after a failed
    regeneration. The fallback never invents a replacement claim; it keeps what
    was supported and states that the rest was removed.
    """
    by_id = {chunk.source_id: chunk for chunk in chunks if chunk.source_id}
    kept: list[str] = []
    for sentence in split_sentences(answer):
        if _is_meta_sentence(sentence):
            kept.append(sentence)
            continue
        citation_ids = extract_citation_ids(sentence)
        if not citation_ids:
            continue
        for source_id in citation_ids:
            chunk = by_id.get(source_id)
            if chunk is None:
                continue
            ok, _overlap = claim_is_supported_by_chunk(sentence, chunk)
            if ok:
                kept.append(sentence)
                break
    return kept


def build_partial_answer(
    answer: str,
    chunks: tuple[RetrievedChunk, ...],
) -> str:
    """Build the honest fallback when verification fails after regeneration."""
    kept = supported_sentences(answer, chunks)
    if not kept:
        return (
            "I could not verify a supported answer from the retrieved passages, "
            "so I am not going to guess. The papers may discuss this, but the "
            "evidence I retrieved does not establish it clearly enough."
        )
    return (
        "I could only verify part of the answer against the retrieved passages. "
        "The supported part is below; unsupported claims have been removed.\n\n"
        + " ".join(kept)
    )


def resolve_citations(
    source_ids: Iterable[str],
    chunks: tuple[RetrievedChunk, ...],
) -> list[dict[str, object]]:
    """Map citation ids to full source metadata for the report/API output."""
    by_id = {chunk.source_id: chunk for chunk in chunks if chunk.source_id}
    out: list[dict[str, object]] = []
    for source_id in source_ids:
        chunk = by_id.get(source_id)
        if chunk is None:
            continue
        out.append(
            {
                "source_id": source_id,
                "doc_id": chunk.doc_id,
                "title": chunk.title,
                "arxiv_id": chunk.arxiv_id,
                "version": chunk.version,
                "page": chunk.page,
                "section": chunk.section,
                "chunk_id": chunk.chunk_id,
                "url": f"https://arxiv.org/abs/{chunk.arxiv_id}{chunk.version}",
            }
        )
    return out


def parse_llm_verification(text: str) -> VerificationResult:
    """Parse the optional LLM verifier's JSON response.

    A malformed response is treated as a failure. That is the safe direction: an
    unparseable verification cannot certify an answer.
    """
    try:
        # Tolerate a model that wraps JSON in a markdown fence.
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        data = json.loads(cleaned)
        supported = bool(data.get("supported", False))
        unsupported = tuple(str(x) for x in data.get("unsupported_claims", []))
        invalid = tuple(str(x) for x in data.get("invalid_citations", []))
        return VerificationResult(
            passed=supported and not unsupported and not invalid,
            issues=tuple(
                [f"LLM verifier: {x}" for x in unsupported]
                + [f"LLM verifier: invalid citation {x}" for x in invalid]
            ),
            invalid_citations=invalid,
            unsupported_claims=unsupported,
            claim_count=1 if supported else 0,
            supported_claim_count=1 if supported else 0,
            method="llm",
        )
    except (ValueError, TypeError, AttributeError) as exc:
        return VerificationResult(
            passed=False,
            issues=(f"LLM verifier returned unparseable output: {exc}",),
            invalid_citations=(),
            unsupported_claims=(),
            claim_count=0,
            supported_claim_count=0,
            method="llm",
        )
