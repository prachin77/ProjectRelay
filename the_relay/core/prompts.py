"""Prompt construction for grounded generation.

PROMPT DESIGN RULES
-------------------
1. **Evidence only.** The model is told, explicitly, that it may not use
   outside knowledge. This is the instruction that makes a citation meaningful.
2. **Compact.** Fewer than ~250 words of instructions. On an 8,000 TPM budget,
   a verbose system prompt is an expensive habit repeated on every call.
3. **Cite every factual sentence.** Inline ``[S1]`` ids are easy for the model
   to emit and easy for the deterministic verifier to check.
4. **Disclose gaps.** The model must say what the papers do not establish rather
   than filling the gap. A supported partial answer is preferred to a confident
   complete one.
5. **No fake page numbers.** Page and section come from retrieval metadata, not
   from the model. The model only cites the id; the report resolves the id.
"""

from __future__ import annotations

from the_relay.types import EvidenceAssessment, RetrievedChunk

SYSTEM_PROMPT = (
    "You are The Relay, a research assistant for exactly three papers. "
    "Answer ONLY from the EVIDENCE provided. Do not use outside knowledge. "
    "Every factual sentence must end with one or more citation ids in square "
    "brackets, e.g. [S1] or [S2, S3]. Never invent a citation id. "
    "If the evidence does not fully answer the question, say exactly what the "
    "papers do support and what they do not establish. Never guess. "
    "If the evidence is insufficient, say so and stop. "
    "Keep the answer under 180 words. Do not include a bibliography; the "
    "application resolves the citation ids to page and section."
)


def build_evidence_block(chunks: tuple[RetrievedChunk, ...]) -> str:
    """Render retrieved passages as a compact, parseable evidence block.

    The format is deliberately simple so that the stand-in can parse it too:

        [S1] (Attention Is All You Need, p. 4, §3.2.1)
        text...

    The bracket id is the only citation token the model is allowed to emit.
    """
    lines: list[str] = []
    for chunk in chunks:
        source_id = chunk.source_id or "S?"
        lines.append(f"[{source_id}] ({chunk.citation_label})")
        lines.append(chunk.text.strip())
        lines.append("")
    return "\n".join(lines).strip()


def _partial_instruction(assessment: EvidenceAssessment) -> str:
    if assessment.level == "partial":
        return (
            "The evidence is PARTIAL. State clearly which part of the question "
            "the papers support and which part they do not. A short supported "
            "answer plus an explicit gap is better than a complete guess."
        )
    return "The evidence is sufficient for a direct answer."


def build_generation_messages(
    question: str,
    chunks: tuple[RetrievedChunk, ...],
    assessment: EvidenceAssessment,
) -> list[dict[str, str]]:
    """Build the messages for one generation call.

    Why one user message rather than a long chat history? Chat history is
    repeated input tokens on every call. This pipeline is stateless per
    question, so there is nothing to remember.
    """
    user = (
        f"QUESTION:\n{question}\n\n"
        f"EVIDENCE:\n{build_evidence_block(chunks)}\n\n"
        f"INSTRUCTION: {_partial_instruction(assessment)}\n"
        "Write the answer now. Cite every factual sentence with [S#]."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_regeneration_messages(
    question: str,
    chunks: tuple[RetrievedChunk, ...],
    assessment: EvidenceAssessment,
    previous_answer: str,
    issues: tuple[str, ...],
) -> list[dict[str, str]]:
    """Build a *single* correction prompt after a verification failure.

    This is bounded: the pipeline allows at most one regeneration. The feedback
    names the exact problem (invalid id, unsupported claim, number mismatch) so
    the model has a chance to fix it without a vague "try again".
    """
    issue_text = "\n".join(f"- {issue}" for issue in issues) or "- unknown"
    user = (
        f"QUESTION:\n{question}\n\n"
        f"EVIDENCE:\n{build_evidence_block(chunks)}\n\n"
        f"YOUR PREVIOUS ANSWER FAILED VERIFICATION:\n{previous_answer}\n\n"
        f"PROBLEMS TO FIX:\n{issue_text}\n\n"
        f"INSTRUCTION: {_partial_instruction(assessment)}\n"
        "Rewrite the answer using only the evidence above. Remove any claim "
        "that is not supported. Cite every remaining factual sentence with "
        "[S#]. If nothing can be supported, say that the papers do not provide "
        "enough evidence."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_verification_messages(
    question: str,
    answer: str,
    chunks: tuple[RetrievedChunk, ...],
) -> list[dict[str, str]]:
    """Prompt for the optional LLM verifier (off by default).

    The output is a small JSON object. The deterministic verifier still runs;
    this call is an *additional* check whose cost must be justified by an A/B
    experiment. It is not the default because it roughly doubles token cost.
    """
    user = (
        f"QUESTION:\n{question}\n\n"
        f"ANSWER TO CHECK:\n{answer}\n\n"
        f"EVIDENCE:\n{build_evidence_block(chunks)}\n\n"
        "Check whether every factual sentence in the answer is supported by the "
        "cited evidence and whether every citation id exists in the evidence. "
        "Return ONLY compact JSON with keys: supported (boolean), "
        "unsupported_claims (array of strings), invalid_citations (array of "
        "strings). Do not add prose."
    )
    return [
        {
            "role": "system",
            "content": (
                "You are a strict citation verifier. You do not answer the "
                "question. You only judge whether the answer is supported by the "
                "provided evidence. Be conservative: if support is unclear, mark "
                "it unsupported."
            ),
        },
        {"role": "user", "content": user},
    ]
