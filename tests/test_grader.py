"""The deterministic grader's four-value contract.

The regression that motivated these tests: an answer that contains all required
facts *and* an honest caveat ("the papers do not provide ...") was graded
``refused_wrongly`` because refusal detection ran first. Facts must decide the
grade for an answerable question.
"""

from __future__ import annotations

from the_relay.eval.grader import explain_grade, grade_answer, has_citation, is_refusal


def test_fully_supported_answer_with_citation_is_correct(make_question) -> None:
    question = make_question()
    answer = "The big model reached a BLEU score of 28.4 on WMT 2014 [S1]."
    assert grade_answer(question, answer, "ok") == "correct"


def test_correct_answer_with_caveat_is_still_correct(make_question) -> None:
    question = make_question()
    answer = (
        "The big model reached a BLEU score of 28.4 on WMT 2014 [S1]. "
        "The papers do not provide a per-language breakdown."
    )
    explanation = explain_grade(question, answer, "ok")
    assert explanation.grade == "correct"
    assert explanation.missing_groups == ()


def test_refusal_on_answerable_question_is_refused_wrongly(make_question) -> None:
    question = make_question()
    answer = "The papers do not contain enough evidence to answer that."
    assert grade_answer(question, answer, "ok") == "refused_wrongly"


def test_answer_missing_a_fact_is_wrong(make_question) -> None:
    question = make_question()
    answer = "The model reached a BLEU score on WMT 2014 [S1]."  # no 28.4
    explanation = explain_grade(question, answer, "ok")
    assert explanation.grade == "wrong"
    assert explanation.missing_groups == ("28.4",)


def test_facts_without_citation_are_wrong(make_question) -> None:
    question = make_question()
    answer = "The big model reached a BLEU score of 28.4 on WMT 2014."
    explanation = explain_grade(question, answer, "ok")
    assert explanation.grade == "wrong"
    assert explanation.cited is False


def test_unanswerable_question_refused_is_refused_correctly(make_question) -> None:
    question = make_question(answerable=False, required_all=())
    answer = "The papers do not say; I have no evidence for that."
    assert grade_answer(question, answer, "ok") == "refused_correctly"


def test_unanswerable_question_answered_is_wrong(make_question) -> None:
    question = make_question(answerable=False, required_all=())
    answer = "Yes, Moshi reports 92.5% accuracy on ImageNet [S1]."
    assert grade_answer(question, answer, "ok") == "wrong"


def test_infrastructure_failure_grade_depends_on_answerability(make_question) -> None:
    answerable = make_question()
    unanswerable = make_question(answerable=False, required_all=())
    assert grade_answer(answerable, "", "rate_limited") == "wrong"
    assert grade_answer(unanswerable, "", "rate_limited") == "refused_correctly"


def test_number_matching_does_not_leak_into_neighbouring_number(make_question) -> None:
    # Required fact is "8"; the answer only contains "28.4" and "18".
    question = make_question(required_all=(("8",),))
    answer = "BLEU was 28.4 with 18 layers [S1]."
    assert grade_answer(question, answer, "ok") == "wrong"
    assert grade_answer(question, "It used 8 GPUs [S1].", "ok") == "correct"


def test_citation_and_refusal_helpers() -> None:
    assert has_citation("A fact [S2].") is True
    assert has_citation("A fact.") is False
    assert is_refusal("I cannot answer that from the evidence.") is True
    assert is_refusal("The model used 8 GPUs [S1].") is False
