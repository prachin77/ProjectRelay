"""Load and validate the hand-written evaluation set.

WHY A JSON FILE?
----------------
The questions are an experimental input, not code. Keeping them in
``evaluation/questions.json`` means a reviewer can read the exact questions,
change one, and re-run the evaluation without touching Python. Every entry also
records why the expected answer is correct, so the grader can be audited instead
of trusted.

THE ``required_all`` CONTRACT
-----------------------------
Each entry has a list of *groups*. A group is a list of acceptable strings.
An answer is correct only when **every group** matches at least one alternative:

    [["28.4"], ["bleu"]]

means the answer must contain the number 28.4 and the word BLEU. This is
deliberately simple and conservative. It is not a semantic grader; the report
says so. It is good enough to catch the failure that matters here: an answer
that is fluent but does not contain the fact that was asked for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EvalQuestion:
    """One question and the evidence needed to grade an answer to it."""

    qid: str
    paper: str
    question: str
    answerable: bool
    required_all: tuple[tuple[str, ...], ...]
    notes: str = ""

    @property
    def expected_grade(self) -> str:
        """The grade a correct system should receive before it answers."""
        return "correct" if self.answerable else "refused_correctly"


def load_questions(path: Path) -> list[EvalQuestion]:
    """Read and validate ``evaluation/questions.json``.

    Validation is strict on purpose. A typo in a ``qid`` would make two result
    rows indistinguishable, and an answerable question with no required facts
    would be impossible to grade honestly.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_questions = data.get("questions")
    if not isinstance(raw_questions, list):
        raise ValueError("evaluation file must contain a 'questions' list")

    questions: list[EvalQuestion] = []
    seen: set[str] = set()
    for item in raw_questions:
        qid = str(item["qid"]).strip()
        if not qid:
            raise ValueError("every question needs a non-empty qid")
        if qid in seen:
            raise ValueError(f"duplicate qid {qid!r}")
        seen.add(qid)

        required_all = tuple(
            tuple(str(term) for term in group)
            for group in item.get("required_all", [])
        )
        answerable = bool(item["answerable"])
        if answerable and not required_all:
            raise ValueError(
                f"answerable question {qid!r} needs at least one required group"
            )
        if any(not group for group in required_all):
            raise ValueError(f"question {qid!r} has an empty required group")

        questions.append(
            EvalQuestion(
                qid=qid,
                paper=str(item["paper"]),
                question=str(item["question"]).strip(),
                answerable=answerable,
                required_all=required_all,
                notes=str(item.get("notes", "")),
            )
        )

    if len(questions) < 15:
        raise ValueError(
            f"the brief requires at least 15 questions; found {len(questions)}"
        )
    return questions


def dataset_summary(questions: list[EvalQuestion]) -> dict[str, object]:
    """Small summary used in traces and the report."""
    by_paper: dict[str, int] = {}
    for question in questions:
        by_paper[question.paper] = by_paper.get(question.paper, 0) + 1
    return {
        "questions": len(questions),
        "answerable": sum(1 for q in questions if q.answerable),
        "unanswerable": sum(1 for q in questions if not q.answerable),
        "by_paper": by_paper,
    }
