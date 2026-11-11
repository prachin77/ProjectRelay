"""Citation normalisation and deterministic claim verification."""

from __future__ import annotations

from the_relay.core.verify import (
    build_partial_answer,
    extract_citation_ids,
    normalise_citation_brackets,
    split_sentences,
    verify_answer,
)


def test_full_width_citation_brackets_are_normalised() -> None:
    assert normalise_citation_brackets("A fact 【S1】.") == "A fact [S1]."
    assert normalise_citation_brackets("A fact ［S2］.") == "A fact [S2]."


def test_citation_ids_are_extracted_in_order() -> None:
    assert extract_citation_ids("A [S1] and B [S3, S2].") == ["S1", "S3", "S2"]


def test_abbreviations_do_not_split_sentences() -> None:
    text = "Deployment (e.g. on a laptop) is possible. It is fast."
    sentences = split_sentences(text)
    assert len(sentences) == 2
    assert sentences[0].endswith("possible.")


def test_supported_claim_passes(make_chunk) -> None:
    chunk = make_chunk(
        "We trained our models on one machine with 8 NVIDIA P100 GPUs."
    )
    answer = "The models were trained on 8 NVIDIA P100 GPUs [S1]."
    result = verify_answer(answer, (chunk,))
    assert result.passed is True
    assert result.claim_count == 1
    assert result.supported_claim_count == 1


def test_invalid_citation_is_rejected(make_chunk) -> None:
    chunk = make_chunk("We trained our models on one machine with 8 NVIDIA P100 GPUs.")
    result = verify_answer("The models used 8 GPUs [S9].", (chunk,))
    assert result.passed is False
    assert result.invalid_citations == ("S9",)


def test_uncited_claim_is_rejected(make_chunk) -> None:
    chunk = make_chunk("We trained our models on one machine with 8 NVIDIA P100 GPUs.")
    result = verify_answer("The models used 8 GPUs.", (chunk,))
    assert result.passed is False
    assert len(result.unsupported_claims) == 1


def test_cited_but_unsupported_number_is_rejected(make_chunk) -> None:
    chunk = make_chunk("We trained our models on one machine with 8 NVIDIA P100 GPUs.")
    result = verify_answer("The models used 64 GPUs [S1].", (chunk,))
    assert result.passed is False
    assert result.supported_claim_count == 0


def test_pure_refusal_passes_without_claims(make_chunk) -> None:
    chunk = make_chunk("We trained our models on one machine with 8 NVIDIA P100 GPUs.")
    result = verify_answer(
        "The papers do not provide enough evidence for that.", (chunk,)
    )
    assert result.passed is True
    assert result.claim_count == 0


def test_partial_answer_keeps_only_supported_sentences(make_chunk) -> None:
    chunk = make_chunk(
        "We trained our models on one machine with 8 NVIDIA P100 GPUs.",
        source_id="S1",
    )
    answer = (
        "The models were trained on 8 NVIDIA P100 GPUs [S1]. "
        "They were cooled with liquid nitrogen."
    )
    partial = build_partial_answer(answer, (chunk,))
    assert "8 NVIDIA P100 GPUs" in partial
    assert "liquid nitrogen" not in partial
