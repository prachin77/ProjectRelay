"""Stage B — deterministic scope filtering.

THE COST PROBLEM THIS SOLVES
----------------------------
A model call is the only expensive part of the pipeline. If a user asks for the
price of a phone, spending 1,500 tokens to have the model say "I only answer
from three papers" is pure waste. The scope filter rejects *obviously* external
questions before retrieval and before any model call.

THE QUALITY TRAP THIS AVOIDS
-----------------------------
Naive keyword filtering is dangerous. A legitimate question may not contain the
paper's title or its exact vocabulary:

    "How does the model decide when two adjacent pieces should be merged?"

That is a Dynamic Chunking question, but it contains no "H-Net" and no
"dynamic chunking". A keyword-only filter would reject it. So this module has
three outcomes, not two:

* ``out_of_scope`` — an explicit external-world marker (price, weather, sport,
  medical advice, coding help, ...). Refuse immediately, zero model calls.
* ``in_scope`` — a strong domain anchor is present. Proceed to retrieval.
* ``ambiguous`` — no anchor, but the question has a technical shape. Proceed to
  retrieval; the evidence gate decides whether the papers can answer it.

The ambiguous path is deliberately generous. It costs a local retrieval (zero
provider tokens) but protects recall. The evidence gate is the real quality
gate; this module only removes questions that are *certainly* outside the
library.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not decide whether the papers contain the answer. That is the evidence
gate's job, after retrieval. It does not call a model. It is pure, deterministic
string logic and costs microseconds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ScopeStatus = Literal["in_scope", "out_of_scope", "ambiguous"]


@dataclass(frozen=True)
class ScopeDecision:
    """Result of the deterministic scope filter."""

    status: ScopeStatus
    reason: str
    matched_rule: str
    # False-positive risk is highest for the explicit external rules. Keeping
    # the rule name lets the evaluation report which rule caused a bad refusal.
    confidence: Literal["high", "medium", "low"] = "medium"


# --- Explicit external-world markers ---------------------------------------
# Each entry is (rule name, compiled regex, human explanation). The regexes are
# intentionally narrow: they match a *request for external information*, not
# the mere appearance of a word. "cost" alone is not enough, because the papers
# discuss compute cost; "price of" is.
_EXTERNAL_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "consumer_price",
        re.compile(
            r"\b(?:price of|stock price|share price|market cap|how much (?:does|do|is) "
            r"(?:a|an|the)?\s*[a-z0-9 ]{1,40}\s*cost|cost in (?:dollars|usd|inr|rupees|euros)|"
            r"salary of|rent for|bitcoin price|ethereum price)\b",
            re.IGNORECASE,
        ),
        "asks for a live price or monetary amount",
    ),
    (
        "current_events",
        re.compile(
            r"\b(?:weather|forecast|temperature in|who won|final score|match score|"
            r"election result|president of|prime minister of|latest news|"
            r"today'?s news|current time|what time is it|date today)\b",
            re.IGNORECASE,
        ),
        "asks about current events, weather, time, or sport",
    ),
    (
        "personal_advice",
        re.compile(
            r"\b(?:should i|diagnose|symptoms?|medicine|dosage|legal advice|"
            r"sue (?:him|her|them|the company)|invest in|which stock)\b",
            re.IGNORECASE,
        ),
        "asks for personal, medical, legal, or financial advice",
    ),
    (
        "coding_help",
        re.compile(
            r"\b(?:write (?:me )?(?:a |some )?(?:python|javascript|java|c\+\+|code)|"
            r"debug (?:my|this) code|stack trace|traceback|install (?:a )?package|"
            r"why does my code|fix my code)\b",
            re.IGNORECASE,
        ),
        "asks for coding help rather than a paper question",
    ),
    (
        "external_fact",
        re.compile(
            r"\b(?:capital of|population of|height of|who is the (?:ceo|author|president)|"
            r"where is .* located|how old is|what language is spoken in)\b",
            re.IGNORECASE,
        ),
        "asks for a fact that is not in the paper library",
    ),
    (
        "citation_format",
        re.compile(
            r"\b(?:cite (?:this|the) paper|apa style|mla style|bibliography|"
            r"references format)\b",
            re.IGNORECASE,
        ),
        "asks about citation formatting, not paper content",
    ),
    (
        "smalltalk",
        re.compile(
            r"^\s*(?:hi|hello|hey|good morning|good afternoon|good evening|"
            r"thanks|thank you|how are you|what'?s up)\s*[.!?]*\s*$",
            re.IGNORECASE,
        ),
        "greeting or small talk",
    ),
    (
        "arithmetic",
        re.compile(r"^\s*(?:what is|calculate|compute)?\s*[\d\s+\-*/().=%]+\s*\??\s*$"),
        "bare arithmetic, not a paper question",
    ),
    (
        "recipe_or_entertainment",
        re.compile(
            r"\b(?:recipe|cook|bake|movie|film|song|lyrics|netflix|football|cricket|"
            r"video game|restaurant)\b",
            re.IGNORECASE,
        ),
        "asks about food or entertainment",
    ),
]

# --- Domain anchors ---------------------------------------------------------
# These are terms that strongly indicate one of the three papers. The list is
# intentionally broad: a false in_scope only costs a local retrieval and an
# honest evidence-based refusal; a false out_of_scope loses a real answer.
_DOMAIN_ANCHORS: frozenset[str] = frozenset(
    """
    attention transformer transformers self-attention multi-head multihead
    encoder decoder encoder-decoder positional encoding embedding embeddings
    qkv query queries key keys value values softmax dot-product dot product
    scaled dot product bleu wmt translation token tokens tokenization
    tokenisation subword bpe byte-pair byte pair sequence sequences
    autoregressive language model models architecture layer layers head heads
    moshi mimi helium kyutai speech speech-text text-to-speech asr tts
    full-duplex duplex streaming audio codec codecs semantic acoustic
    waveform wavlm rvq vector quantization quantizer quantisation
    chunking chunk chunks dynamic chunking h-net hnet hierarchy hierarchical
    u-net unet subsampling downsampling upsampling pooling merge merging
    rqd ratio loss reconstruction reconstruction loss fsq frame rate
    kernel convolution convolutional recurrent rnn lstm state space ssm
    mamba attention mechanism mechanism training inference latency
    perplexity wer word error rate cosine similarity contrastive
    distillation distillation loss teacher student fine-tuning pretraining
    pretrain benchmark dataset datasets ablation ablations compute
    parameters parameter count flops gpu tpu memory context window
    """.split()
)

# Words that make a question look technical even without an anchor. Used only
# for the "ambiguous" path, never for an automatic in_scope decision.
_TECHNICAL_SHAPE = re.compile(
    r"^\s*(?:what|how|why|when|where|which|who|does|do|is|are|can|could|"
    r"explain|describe|compare|summari[sz]e|list|define|give|tell)\b",
    re.IGNORECASE,
)

_STOPWORDS = frozenset(
    "a an the and or but if then of in on at to for from by with without into "
    "about over under between among during before after is are was were be been "
    "being do does did have has had can could should would will may might".split()
)


def _content_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9\-']*", text.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


def _contains_anchor(text: str) -> str | None:
    """Return the first domain anchor found, or None.

    We check whole tokens and also simple hyphen/space variants: "multi-head"
    and "multi head" both map to the anchor "multi-head"/"multihead".
    """
    tokens = _content_tokens(text)
    token_set = set(tokens)
    for anchor in _DOMAIN_ANCHORS:
        if anchor in token_set:
            return anchor
    # Also catch anchors embedded in hyphenated compounds.
    for token in token_set:
        for part in re.split(r"[-_/]", token):
            if part in _DOMAIN_ANCHORS:
                return part
    return None


def filter_scope(question: str) -> ScopeDecision:
    """Classify a question as in-scope, out-of-scope, or ambiguous.

    Order matters:
    1. Empty / tiny input -> out_of_scope.
    2. An explicit external marker -> out_of_scope, *unless* a domain anchor is
       present. The anchor exception matters: "What does the paper say about
       the cost of training the Transformer?" is a legitimate paper question
       that happens to contain "cost".
    3. A domain anchor -> in_scope.
    4. A technical question shape -> ambiguous (let retrieval decide).
    5. Otherwise -> out_of_scope.
    """
    text = (question or "").strip()
    if not text:
        return ScopeDecision("out_of_scope", "empty question", "empty", "high")
    if len(text) < 3:
        return ScopeDecision("out_of_scope", "question is too short", "too_short", "high")

    anchor = _contains_anchor(text)
    for rule_name, pattern, explanation in _EXTERNAL_RULES:
        if pattern.search(text):
            if anchor is not None:
                # The question mentions a paper concept, so it may still be
                # answerable (e.g. "training cost" in the papers). Do not
                # reject it here; let retrieval and the evidence gate decide.
                return ScopeDecision(
                    "in_scope",
                    f"external marker {rule_name!r} overridden by domain anchor {anchor!r}",
                    f"anchor:{anchor}",
                    "medium",
                )
            return ScopeDecision(
                "out_of_scope",
                explanation,
                rule_name,
                "high" if rule_name in {"smalltalk", "arithmetic"} else "medium",
            )

    if anchor is not None:
        return ScopeDecision(
            "in_scope",
            f"domain anchor {anchor!r} found",
            f"anchor:{anchor}",
            "high",
        )

    if _TECHNICAL_SHAPE.search(text) and len(_content_tokens(text)) >= 3:
        # No anchor, but a plausible research question. Retrieval is local and
        # free in provider tokens, so the safe choice is to let evidence decide.
        return ScopeDecision(
            "ambiguous",
            "no domain anchor, but the question has a technical shape; "
            "retrieval and the evidence gate will decide",
            "technical_shape",
            "low",
        )

    return ScopeDecision(
        "out_of_scope",
        "no domain anchor and no technical question shape",
        "no_anchor",
        "medium",
    )


def refusal_message(decision: ScopeDecision) -> str:
    """The user-facing text for an out-of-scope question.

    It never calls the model and never pretends to know the answer. It names
    the library and explains the limit, which is more useful than a bare "no".
    """
    return (
        "I can only answer questions grounded in three research papers: "
        "*Attention Is All You Need*, *Moshi: a speech-text foundation model for "
        "real-time dialogue*, and *Dynamic Chunking for End-to-End Hierarchical "
        "Sequence Modeling*.\n\n"
        f"This question looks outside that library ({decision.reason}), so I "
        "have not sent it to a model. Rephrasing it as a question about one of "
        "those papers will let me retrieve evidence and cite it."
    )
