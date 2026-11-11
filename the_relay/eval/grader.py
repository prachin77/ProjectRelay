"""Deterministic grading of one answer against one hand-written question.

WHAT THIS GRADER IS
-------------------
A transparent keyword/group checker, not a language model. Each question lists
required groups and every group must be present. A correct answer must also
contain at least one inline citation, because an uncited claim is not a grounded
answer for this assignment.

WHAT IT IS NOT
--------------
It cannot tell whether the cited sentence actually supports the claim. The
pipeline's verifier does a separate lexical check, and both are weaker than a
human reader. The report states this limitation and the evaluation file records
the exact expected fact so a human can audit every grade.

INFRASTRUCTURE FAILURES
-----------------------
A rate-limited, timed-out, or errored request delivered no answer. The brief's
raw file has only four grade values, so the choice is made explicitly:

* answerable question  -> ``wrong`` (the user did not receive the correct fact);
* unanswerable question -> ``refused_correctly`` (no fabricated answer was sent).

This rule is documented in the report so the quality percentages can be read
correctly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from the_relay.eval.dataset import EvalQuestion

_CITATION_RE = re.compile(r"\[S\d+")

# Phrases that mean "I am not answering this from the evidence". They are
# deliberately specific: a generic "not" must not turn a real answer into a
# refusal. The quota/queue messages are included because those responses also
# contain no fabricated answer.
_REFUSAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"do(?:es)? not contain enough (?:evidence|information)",
        r"not enough (?:evidence|information)",
        r"could not verify a supported answer",
        r"could only verify part of the answer",
        r"only partially support",
        r"can only answer questions grounded in three research papers",
        r"outside that library",
        r"papers? (?:do|does) not (?:say|provide|establish|report|contain)",
        r"\bno evidence\b",
        r"cannot answer",
        r"not establish",
        r"(?:will|going to) not guess",
        r"not processed",
    )
)


@dataclass(frozen=True)
class GradeExplanation:
    """Why a particular grade was assigned (written to the trace, not the CSV)."""

    grade: str
    reason: str
    matched_groups: tuple[str, ...] = ()
    missing_groups: tuple[str, ...] = ()
    cited: bool = False


def normalise(text: str) -> str:
    """Lower-case and collapse whitespace for matching."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def has_citation(answer: str) -> bool:
    """True when the answer contains at least one ``[S#]`` marker."""
    return bool(_CITATION_RE.search(answer or ""))


def is_refusal(answer: str) -> bool:
    """True when the answer says the evidence does not support an answer."""
    text = normalise(answer)
    return any(pattern.search(text) for pattern in _REFUSAL_PATTERNS)


def _term_present(term: str, text: str) -> bool:
    """Match a required term without letting a number match its neighbours.

    ``8`` must not match inside ``28.4`` or ``18``. A plain substring check
    would make the GPU-count question pass on the BLEU number, which would be a
    silent grading bug.
    """
    term = normalise(term)
    if not term:
        return False
    if re.fullmatch(r"[\d.]+", term):
        pattern = rf"(?<![\d.]){re.escape(term)}(?![\d.])"
        return re.search(pattern, text) is not None
    if re.fullmatch(r"\d+[a-z]", term):
        pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return term in text


def explain_grade(
    question: EvalQuestion,
    answer: str,
    status: str,
) -> GradeExplanation:
    """Return the grade plus the evidence behind it."""
    text = normalise(answer)
    cited = has_citation(answer)

    if status != "ok":
        grade = "wrong" if question.answerable else "refused_correctly"
        return GradeExplanation(
            grade=grade,
            reason=(
                f"request status was {status!r}, so no grounded answer was "
                "delivered"
            ),
            cited=cited,
        )

    refused = is_refusal(answer)

    if not question.answerable:
        if refused:
            return GradeExplanation(
                grade="refused_correctly",
                reason="the question is not answerable from the library and the "
                "assistant refused",
                cited=cited,
            )
        return GradeExplanation(
            grade="wrong",
            reason="the question is not answerable from the library but the "
            "assistant produced an answer instead of refusing",
            cited=cited,
        )

    if refused:
        return GradeExplanation(
            grade="refused_wrongly",
            reason="the question is answerable but the assistant refused",
            cited=cited,
        )

    matched: list[str] = []
    missing: list[str] = []
    for group in question.required_all:
        if any(_term_present(term, text) for term in group):
            matched.append(" | ".join(group))
        else:
            missing.append(" | ".join(group))

    if missing:
        return GradeExplanation(
            grade="wrong",
            reason="the answer is missing required evidence",
            matched_groups=tuple(matched),
            missing_groups=tuple(missing),
            cited=cited,
        )

    if not cited:
        return GradeExplanation(
            grade="wrong",
            reason="the required facts are present but the answer has no citation",
            matched_groups=tuple(matched),
            missing_groups=(),
            cited=False,
        )

    return GradeExplanation(
        grade="correct",
        reason="all required facts and at least one citation are present",
        matched_groups=tuple(matched),
        missing_groups=(),
        cited=True,
    )


def grade_answer(question: EvalQuestion, answer: str, status: str) -> str:
    """Convenience wrapper returning only the four-value grade."""
    return explain_grade(question, answer, status).grade
