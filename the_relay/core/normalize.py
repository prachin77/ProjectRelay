"""Stage A — deterministic normalisation of the incoming question.

WHY THIS STAGE EXISTS
---------------------
Two questions that mean the same thing should hit the same cache entry. But
"normalise" is a dangerous verb: if you lowercase everything and strip
punctuation carelessly you will happily turn

    "the model is NOT supported on CPU"

into something that matches

    "the model is supported on CPU"

and serve a wrong answer from cache. So this module follows one rule:

    **Normalise only what cannot carry meaning, and extract (never destroy)
    everything that can.**

Concretely we normalise these, because they carry no meaning:

* Unicode compatibility forms, ligatures, exotic quotes and dashes
* case (for the *key* only -- the displayed question is kept verbatim)
* runs of whitespace
* trailing/leading punctuation

And we **extract** these, because they do carry meaning, and compare them as
sets rather than destroying them during normalisation:

* numbers ("28.4", "3.5 days", "160ms")
* negation polarity ("not", "never", "without", "isn't", ...)
* named / technical entities ("Moshi", "H-Net", "English-to-German", "BLEU")
* intent class (definition / comparison / limitation / numeric / method)

WHAT THIS STAGE DOES *NOT* DO
-----------------------------
It does not decide whether a question is answerable, and it never calls a
model. Both of those are later stages. Normalisation is pure string work, so
it costs ~0 ms and ~0 tokens.

COST / QUALITY / LATENCY
------------------------
* Cost: none. No network, no tokens.
* Quality: **high impact.** This is where a careless implementation creates
  cache false positives, which are the single most damaging failure mode in
  the whole system (a confidently wrong answer, served instantly).
* Latency: microseconds.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Vocabulary tables
# ---------------------------------------------------------------------------

# Words that mark a question's *shape* rather than its content. Only used to
# classify intent and to avoid treating sentence-initial capitals as names.
_STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or but if then than that this these those of in on at to for
    from by with without into about over under between among during before
    after above below up down out off again further once here there when where
    why how all any both each few more most other some such no nor not only own
    same so too very can will just should now is are was were be been being do
    does did doing have has had having i you he she it we they me him her us
    them my your his its our their what which who whom whose
    """.split()
)

# Sentence-initial capitals that look like names but are not. Removing these
# prevents "What ... Transformer ..." and "Explain ... Transformer ..."
# from producing different entity sets purely because the sentence starts
# with a different verb.
_NOT_ENTITIES: frozenset[str] = frozenset(
    """
    what how why when where who which whose does do did is are was were can
    could would should shall will may might must explain describe list give
    compare tell name define summarise summarize state identify provide
    according based within using via
    """.split()
)

# Negation cues. Deliberately includes contractions because "doesn't" and
# "does not" must produce the same polarity.
_NEGATION_PATTERN = re.compile(
    r"\b(?:not|no|never|none|nobody|nothing|neither|nor|without|cannot|"
    r"can't|won't|isn't|aren't|wasn't|weren't|doesn't|don't|didn't|"
    r"hasn't|haven't|hadn't|shouldn't|wouldn't|couldn't|fails?\s+to|"
    r"unable\s+to|lacks?|lacking)\b",
    re.IGNORECASE,
)

# Numbers, including decimals, thousands separators and simple fractions.
# Kept as *strings* (normalised) because 8.0 and 8 are the same value but
# "8k" and "8000" should still be recognisably related; exact-value parsing
# would introduce floating point comparison noise for no benefit here.
_NUMBER_PATTERN = re.compile(r"\b\d+(?:[.,]\d+)*\s*(?:%|k|m|bn|ms|s|gb|gpu|gpus)?\b", re.IGNORECASE)

# A "name-like" token: starts with a capital, may contain digits, and may be
# hyphenated or apostrophised ("H-Net", "GPT-OSS", "QK^T"-style handled below).
_CAPITALISED_PATTERN = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:[-'’][A-Za-z0-9]+)*\b")

# All-caps acronyms anywhere in the text ("BLEU", "BPE", "TPM").
_ACRONYM_PATTERN = re.compile(r"\b[A-Z]{2,}[0-9]*\b")

# Symbols that are meaningful in these particular papers. Named so that the
# reason is obvious to a reader: the Transformer paper is full of them.
_TECH_SYMBOL_PATTERN = re.compile(r"\b[A-Z]\s*\^\s*[A-Z]\b|\bQK\^?T\b")

_ALLOWED_CHARS = re.compile(r"[^a-z0-9\s\-'/.#^+=%_&]")

IntentClass = Literal[
    "definition", "comparison", "limitation", "numeric", "method", "other"
]

# Ordered cues. Order is significant: a question that asks to *compare* is a
# comparison even if it also says "explain".
_INTENT_CUES: list[tuple[IntentClass, tuple[str, ...]]] = [
    (
        "comparison",
        ("compare", "comparison", "differ", "difference", "differences",
         "versus", " vs ", "vs.", "better than", "worse than", "contrast"),
    ),
    (
        "limitation",
        ("limitation", "limitations", "drawback", "drawbacks", "weakness",
         "weaknesses", "shortcoming", "shortcomings", "fails", "fail to",
         "problem", "problems", "downside"),
    ),
    (
        "numeric",
        ("how many", "how much", "what percentage", "what fraction",
         "what value", "what score", "number of", "how fast", "how long",
         "what is the bleu", "what is the latency", "how large"),
    ),
    (
        "method",
        ("how does", "how do", "how is", "how are", "in what way",
         "mechanism", "procedure", "algorithm", "step by step"),
    ),
    (
        "definition",
        ("what is", "what are", "what does", "define", "definition",
         "explain", "describe", "summarise", "summarize", "meaning of"),
    ),
]


@dataclass(frozen=True)
class NormalizedQuestion:
    """Everything later stages need to know about the question's wording.

    Attributes
    ----------
    original:
        Exactly what the user typed. Never modified, always displayed back.
    normalized:
        The deterministic form used to build cache keys and to embed.
    content_terms:
        Lower-cased words with stopwords removed. Used for lexical overlap
        checks and for the scope filter.
    numbers:
        Sorted set of numeric literals found in the question.
    negated:
        ``True`` if the question contains any negation cue. Coarse on purpose:
        the guard only needs to know "did the polarity flip?".
    entities:
        Lower-cased name-like and acronym tokens.
    intent:
        Coarse question shape, used only by the semantic-cache guard.
    """

    original: str
    normalized: str
    content_terms: tuple[str, ...] = field(default_factory=tuple)
    numbers: tuple[str, ...] = field(default_factory=tuple)
    negated: bool = False
    entities: tuple[str, ...] = field(default_factory=tuple)
    intent: IntentClass = "other"

    @property
    def content_signature(self) -> str:
        """Stable fingerprint of the *meaningful* parts of the question.

        Used by the equivalence guard as one of its checks: two questions with
        the same signature are very likely the same question.
        """
        payload = "|".join(
            [
                " ".join(self.content_terms),
                ",".join(self.numbers),
                "1" if self.negated else "0",
                ",".join(self.entities),
                self.intent,
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @property
    def embedding_text(self) -> str:
        """Text handed to the embedding model.

        We embed the *normalised* form rather than the raw one so that
        cosmetic differences (capitalisation, trailing punctuation) do not
        move the vector around. We do not strip stopwords here: embedding
        models are trained on natural sentences and degrade on keyword soup.
        """
        return self.normalized


def normalize_question(raw: str) -> NormalizedQuestion:
    """Turn raw user input into a :class:`NormalizedQuestion`.

    The function is intentionally pure and deterministic: the same input
    always produces the same output, on any machine, with no I/O. That is what
    makes exact-cache keys trustworthy.
    """
    original = raw if isinstance(raw, str) else str(raw)

    # 1. Unicode compatibility normalisation.
    #    NFKC folds ligatures ("ﬁ" -> "fi") and full-width characters, which
    #    is what you want when text has been pasted from a PDF.
    text = unicodedata.normalize("NFKC", original)

    # 2. Typographic quotes and dashes -> ASCII.
    #    Done per character, not with a regex, so the mapping is auditable.
    translation = {
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
        "\u201c": '"', "\u201d": '"', "\u201e": '"',
        "\u2013": "-", "\u2014": "-", "\u2212": "-",
        "\u00a0": " ",
    }
    text = "".join(translation.get(ch, ch) for ch in text)

    # 3. Collapse whitespace. Newlines inside a question are never meaningful.
    text = re.sub(r"\s+", " ", text).strip()

    # 4. Lower-case FOR THE KEY. Note we keep `original` untouched and we
    #    derive entities/numeric tokens from the *pre-lowercase* text below,
    #    so case information survives where it matters.
    lowered = text.lower()

    # 5. Drop characters that cannot disambiguate a question, but KEEP
    #    hyphens, slashes, dots, carets, pluses, hashes and percent signs --
    #    they appear in real technical terms (H-Net, seq2seq, 3.5, QK^T, 28.4%).
    lowered = _ALLOWED_CHARS.sub(" ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip(" .,;:!?").strip()

    # 6. Content terms: lower-cased, stopwords removed, order preserved.
    terms = tuple(
        token
        for token in re.findall(r"[a-z0-9][a-z0-9\-'/.^+=%_&]*", lowered)
        if token not in _STOPWORDS and len(token) > 1
    )

    # 7. Numbers, taken from the case-preserved text and lower-cased.
    numbers = tuple(sorted({m.group(0).strip().lower() for m in _NUMBER_PATTERN.finditer(text)}))

    # 8. Negation polarity.
    negated = bool(_NEGATION_PATTERN.search(text))

    # 9. Entities: capitalised words (minus sentence-starter verbs), acronyms,
    #    and technical symbol forms. Lower-cased so "MOSHI" == "Moshi".
    entities: set[str] = set()
    for match in _CAPITALISED_PATTERN.finditer(text):
        token = match.group(0)
        if token.lower() in _NOT_ENTITIES:
            continue
        if token.lower() in _STOPWORDS:
            continue
        entities.add(token.lower())
    for match in _ACRONYM_PATTERN.finditer(text):
        entities.add(match.group(0).lower())
    for match in _TECH_SYMBOL_PATTERN.finditer(text):
        entities.add(re.sub(r"\s+", "", match.group(0)).lower())

    # 10. Intent class. Padded with spaces so " vs " cues match at the edges.
    padded = f" {lowered} "
    intent: IntentClass = "other"
    for label, cues in _INTENT_CUES:
        if any(cue in padded for cue in cues):
            intent = label
            break

    return NormalizedQuestion(
        original=original,
        normalized=lowered,
        content_terms=terms,
        numbers=numbers,
        negated=negated,
        entities=tuple(sorted(entities)),
        intent=intent,
    )
