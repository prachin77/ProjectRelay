"""Paper targeting and the corpus-level topic-presence check.

These tests pin the behaviour that stopped a Moshi passage from answering a
question about the 2017 Transformer, and that forces an honest refusal when the
named paper never mentions the question's topic.
"""

from __future__ import annotations

from the_relay.rag.targeting import (
    absent_terms_from_corpus,
    detect_paper_target,
    distinctive_terms,
    stem_term,
)


def test_moshi_is_detected_as_strong_target() -> None:
    target = detect_paper_target("How large is the audio dataset used to train Moshi?")
    assert target is not None
    assert target.doc_id == "moshi"
    assert target.explicit is True


def test_h_net_is_dynamic_chunking_paper() -> None:
    target = detect_paper_target("What does the H-Net routing module replace?")
    assert target is not None
    assert target.doc_id == "dynamic_chunking"
    assert target.explicit is True


def test_named_transformer_paper_is_explicit() -> None:
    target = detect_paper_target("In the Transformer paper, how many attention heads?")
    assert target is not None
    assert target.doc_id == "attention"
    assert target.explicit is True


def test_bare_transformer_is_only_a_weak_hint() -> None:
    target = detect_paper_target("How many attention heads does the Transformer use?")
    assert target is not None
    assert target.doc_id == "attention"
    # A weak hint must not authorize a corpus-absence refusal later.
    assert target.explicit is False


def test_rq_transformer_belongs_to_moshi_not_attention() -> None:
    target = detect_paper_target("What is the RQ-Transformer in Moshi?")
    assert target is not None
    assert target.doc_id == "moshi"


def test_plural_library_wording_disables_narrowing() -> None:
    assert detect_paper_target("Which of these papers reports the lowest latency?") is None


def test_cross_paper_comparison_disables_narrowing() -> None:
    assert detect_paper_target("Compare Moshi and the Transformer on latency") is None


def test_no_paper_named_and_no_weak_hint_returns_none() -> None:
    assert detect_paper_target("What is the capital of France?") is None


def test_distinctive_terms_drop_framing_words_and_numbers() -> None:
    terms = distinctive_terms("How does the paper describe 3 important results?")
    assert "paper" not in terms
    assert "describe" not in terms
    assert "3" not in terms


def test_absent_terms_detects_topic_missing_from_body() -> None:
    body = {"dynamic", "chunk", "h", "net", "routing", "module", "language"}
    absent = absent_terms_from_corpus(
        "How does the H-Net use ImageNet classification accuracy?",
        body,
    )
    assert "imagenet" in absent
    assert "classification" in absent


def test_absent_terms_is_empty_when_topic_is_present() -> None:
    body = {"audio", "dataset", "train", "moshi", "large"}
    assert absent_terms_from_corpus("How large is Moshi's audio dataset?", body) == ()


def test_stemming_matches_plural_and_gerund_forms() -> None:
    assert stem_term("masking") == stem_term("mask")
    assert stem_term("tokens") == stem_term("token")
