"""Deterministic paper targeting.

WHY THIS MODULE EXISTS
----------------------
All three papers share vocabulary: every one of them discusses Transformers,
training, tokens and attention. A pure lexical retriever therefore happily
returns a Moshi passage for a question about the original Transformer. That is
not a retrieval *bug* in the similarity sense -- the words really do occur --
but it is a quality failure, and it wastes prompt tokens on text that cannot
answer the question.

This module answers one narrow question before retrieval:

    "Does the wording of the question name exactly one of the three papers?"

If it does, retrieval is restricted to that paper. This is the retrieval
equivalent of a human opening the right PDF first. It is deterministic, costs
no tokens, and is logged so a wrong target is visible in the trace.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* It does not decide whether the paper *answers* the question. That is still the
  evidence gate's job.
* It does not treat the bare word "Transformer" as a strong identifier, because
  Moshi and Dynamic Chunking both contain Transformers. A bare "Transformer" is
  only a weak hint, used when no other paper is named.
* It does not target when the question explicitly compares papers, or refers to
  the library in the plural ("these papers"). A comparison needs evidence from
  more than one document.

The distinction between a *strong* name ("the Moshi paper", "H-Net") and a
*weak* hint ("Transformer") matters later: only a strong, explicit paper
reference allows the evidence gate to conclude "the named paper never mentions
this topic" and refuse. A weak hint must not cause that refusal, because the
question may be using the word generically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from the_relay.rag.index import tokenize

# Names that identify a paper unambiguously. Order is precedence order:
# "RQ-Transformer" is a Moshi component, not the 2017 paper, so Moshi is checked
# before the attention hints.
_STRONG_NAMES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "moshi",
        (
            "moshi",
            "mimi",
            "helium",
            "inner monologue",
            "rq-transformer",
            "rq transformer",
        ),
    ),
    (
        "dynamic_chunking",
        (
            "dynamic chunking",
            "h-net",
            "hnet",
            "dechunking",
            "routing module",
        ),
    ),
    (
        "attention",
        (
            "attention is all you need",
            "transformer paper",
            "attention paper",
            "scaled dot-product attention",
            "multi-head attention",
            "positional encoding",
        ),
    ),
)

# A bare "Transformer" is a weak hint for the 2017 paper. It is only used when
# no strong name appears anywhere in the question.
_WEAK_ATTENTION_HINTS = (
    "transformer",
    "self-attention",
    "bleu",
    "wmt",
    "encoder-decoder",
)

# Plural / cross-paper wording. When present, retrieval must stay unrestricted.
_PLURAL_LIBRARY_RE = re.compile(
    r"\b(?:these papers|the papers|all three|both papers|across the papers|"
    r"each paper|the three papers|the library|the documents)\b",
    re.IGNORECASE,
)
_CROSS_PAPER_RE = re.compile(
    r"\b(?:compare|comparison|contrast|differ|difference|versus|vs\.?)\b",
    re.IGNORECASE,
)

# Words that describe the *shape of the question* rather than its topic. They
# are excluded before the "does the named paper mention this?" check. Without
# this list, a perfectly good question would be refused because it contains
# "describe", "replace" or "role" and the paper happens not to use that verb.
_FRAMING_TERMS: frozenset[str] = frozenset(
    """
    paper papers report reports reported result results study studies author
    authors show shows showed shown propose proposes proposed present presents
    presented describe describes described explain explains discuss discussed
    mention mentions say says state states tell tells give gives provide
    provides according question answer answers library three section paper
    use uses used using work works approach approaches method methods model
    models main kind type types way ways first second third next last
    mathematical role play plays replace replaces replaced decide decides
    decided handle handles handled input inputs output outputs data dataset
    dataset large small good better best important different same new
    """.split()
)


@dataclass(frozen=True)
class PaperTarget:
    """The paper a question appears to be about."""

    doc_id: str
    explicit: bool
    reason: str


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """Whole-phrase containment that tolerates hyphen/space variants.

    ``H-Net`` and ``H Net`` are the same name for this purpose, and a match must
    not occur inside a longer word (``helium`` must not match ``heliumbased``).
    """
    variants = {phrase, phrase.replace("-", " "), phrase.replace(" ", "-")}
    for variant in variants:
        if re.search(rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])", haystack):
            return True
    return False


def detect_paper_target(question: str) -> PaperTarget | None:
    """Return the single paper the question names, or ``None``.

    ``None`` means "leave retrieval unrestricted": either no paper was named,
    or the question compares papers and therefore needs more than one.
    """
    lowered = question.lower()

    strong = [
        doc_id
        for doc_id, names in _STRONG_NAMES
        if any(_contains_phrase(lowered, name) for name in names)
    ]
    # Two strong names means a cross-paper question. Do not narrow.
    if len(strong) > 1:
        return None
    if len(strong) == 1:
        doc_id = strong[0]
        # "Moshi versus the Transformer" is a real cross-paper comparison. The
        # definite article matters: "a BPE-tokenized Transformer" inside the
        # Dynamic Chunking paper is a baseline model, not the 2017 paper.
        if doc_id != "attention" and _CROSS_PAPER_RE.search(lowered):
            if re.search(r"\bthe transformer\b", lowered):
                return None
        if _PLURAL_LIBRARY_RE.search(lowered):
            return None
        return PaperTarget(
            doc_id=doc_id,
            explicit=True,
            reason=f"question names the {doc_id} paper explicitly",
        )

    if _PLURAL_LIBRARY_RE.search(lowered):
        return None

    if any(hint in lowered for hint in _WEAK_ATTENTION_HINTS):
        return PaperTarget(
            doc_id="attention",
            explicit=False,
            reason=(
                "question uses Transformer-specific vocabulary but does not name "
                "a paper; treated as a weak hint"
            ),
        )
    return None


def _stem(term: str) -> str:
    """A tiny, deterministic stemmer for presence checks.

    It only removes common English suffixes. This is intentionally crude: its
    job is to stop "masking" and "mask" being treated as different topics, not
    to be linguistically correct.
    """
    for suffix in (
        "ization",
        "ations",
        "ation",
        "ingly",
        "edly",
        "ing",
        "ies",
        "ied",
        "ed",
        "es",
        "s",
    ):
        if len(term) > len(suffix) + 3 and term.endswith(suffix):
            return term[: -len(suffix)]
    return term


def stem_term(term: str) -> str:
    """Public alias used by the stand-in's sentence scorer."""
    return _stem(term)


def distinctive_terms(question: str) -> tuple[str, ...]:
    """Content terms that describe the question's topic, not its shape.

    Numbers, single characters and framing words are removed. The result is
    used only to check presence in one paper's body text; it is not a semantic
    representation of the question.
    """
    seen: set[str] = set()
    out: list[str] = []
    for term in tokenize(question):
        if len(term) < 3 or term.isdigit() or term in _FRAMING_TERMS:
            continue
        if term not in seen:
            seen.add(term)
            out.append(term)
    return tuple(out)


def absent_terms_from_corpus(
    question: str,
    body_terms: set[str],
) -> tuple[str, ...]:
    """Return distinctive question terms whose stem never occurs in the paper.

    ``body_terms`` must be the tokenised body of one paper, excluding reference
    lists and acknowledgements. A term found only in a bibliography is not
    evidence that the paper *studies* the topic (ImageNet is the motivating
    example), which is exactly why the caller filters back matter first.
    """
    corpus_stems = {_stem(term) for term in body_terms}
    return tuple(
        term for term in distinctive_terms(question) if _stem(term) not in corpus_stems
    )
