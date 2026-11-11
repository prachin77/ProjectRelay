"""Evidence-gate behaviour: the cheap, deterministic refusal filter."""

from __future__ import annotations

from the_relay.core.evidence import assess_evidence, refusal_message


def test_no_chunks_is_insufficient(make_chunk, settings_factory) -> None:
    assessment = assess_evidence("What is dynamic chunking?", (), settings_factory())
    assert assessment.level == "insufficient"
    assert "no passages" in assessment.reason


def test_absent_topic_terms_force_refusal(make_chunk, settings_factory) -> None:
    # The retrieved chunk is topically adjacent; the corpus check says the
    # named paper never mentions ImageNet at all, so no re-retrieval can help.
    chunk = make_chunk("We train on a language modelling corpus with a routing module.")
    assessment = assess_evidence(
        "What ImageNet accuracy did the H-Net reach?",
        (chunk,),
        settings_factory(),
        absent_topic_terms=("imagenet", "accuracy"),
        named_paper="dynamic_chunking",
    )
    assert assessment.level == "insufficient"
    assert "imagenet" in assessment.reason
    assert "enough evidence" in refusal_message(assessment)


def test_strong_single_passage_is_sufficient(make_chunk, settings_factory) -> None:
    question = "Why divide dot products by the square root of d_k?"
    text = (
        "Scaled dot-product attention divides the dot products of the query "
        "with all keys by the square root of d_k to keep the dot products from "
        "growing large and pushing the softmax into small-gradient regions."
    )
    chunk = make_chunk(text, relevance=0.8)
    assessment = assess_evidence(question, (chunk,), settings_factory())
    assert assessment.level == "sufficient"
    assert assessment.top1_score == 0.8


def test_numeric_question_without_a_number_is_partial(make_chunk, settings_factory) -> None:
    question = "What BLEU score did the big Transformer achieve?"
    text = (
        "The big Transformer achieved a strong BLEU score on the translation "
        "task and outperformed the previous published models."
    )
    chunk = make_chunk(text, relevance=0.8)
    assessment = assess_evidence(question, (chunk,), settings_factory())
    assert assessment.level == "partial"
    assert assessment.numeric_requested is True
    assert assessment.numeric_found is False


def test_comparison_from_one_document_is_partial(make_chunk, settings_factory) -> None:
    question = "Compare the Transformer and Moshi on latency."
    text = (
        "The Transformer reports translation quality and training cost; the "
        "comparison with Moshi latency is not part of this passage."
    )
    chunk = make_chunk(text, relevance=0.8)
    assessment = assess_evidence(question, (chunk,), settings_factory())
    assert assessment.level == "partial"
    assert "one paper" in assessment.reason


def test_weak_evidence_is_insufficient(make_chunk, settings_factory) -> None:
    chunk = make_chunk("Unrelated text about bibliographies.", relevance=0.01)
    assessment = assess_evidence(
        "What is the exact latency of the Moshi model?",
        (chunk,),
        settings_factory(),
    )
    assert assessment.level == "insufficient"
