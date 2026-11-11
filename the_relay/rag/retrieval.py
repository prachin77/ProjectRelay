"""The retrieval *tool* used by the pipeline.

WHY A SEPARATE TOOL LAYER?
--------------------------
The brief asks for "at least one tool call". Keeping retrieval behind a small
typed interface (``RetrievalTool.retrieve``) makes the tool boundary explicit:
the orchestrator calls it, it returns passages plus metadata, and every call is
traced. It also makes the bounded re-retrieval loop easy to implement without
duplicating retrieval logic.

COST DISCIPLINE
---------------
Retrieval is local and free in provider tokens, but it is not free in *prompt*
tokens: every chunk put into the generation prompt costs money next minute. This
layer enforces the preset's ``max_context_tokens`` cap and truncates the last
chunk rather than silently exceeding it. That is a direct cost control, and it
is visible in the trace (``context_tokens``).
"""

from __future__ import annotations

import time

from the_relay.config import (
    RetrievalConfig,
    RetrievalPreset,
    Settings,
)
from the_relay.core.tokens import estimate_tokens
from the_relay.rag.index import content_terms, document_body_terms, search
from the_relay.types import RetrievalResult, RetrievedChunk


class RetrievalTool:
    """Typed retrieval interface over the local hybrid index."""

    def __init__(self, settings: Settings, retrieval_config: RetrievalConfig) -> None:
        self.settings = settings
        self.retrieval_config = retrieval_config

    def preset(self, name: str) -> RetrievalPreset:
        return self.retrieval_config.get(name)

    def retrieve(
        self,
        question: str,
        preset_name: str,
        *,
        loop: int = 0,
        top_k_override: int | None = None,
        query_override: str | None = None,
        doc_filter: str | None = None,
    ) -> RetrievalResult:
        """Retrieve passages for ``question``.

        ``loop`` is recorded so the trace can show whether an answer came from
        the first retrieval or from a bounded re-retrieval. ``query_override``
        lets the evidence loop expand the query without mutating the user's
        original question. ``doc_filter`` restricts retrieval to one paper when
        the question names it (see ``rag/targeting.py``).
        """
        preset = self.preset(preset_name)
        query = query_override or question
        started = time.monotonic()
        chunks = search(
            self.settings,
            preset,
            query,
            fusion_pool=self.retrieval_config.fusion_pool,
            rrf_k=self.retrieval_config.rrf_k,
            top_k_override=top_k_override,
            doc_filter=doc_filter,
        )
        chunks = self._fit_context(chunks, preset.max_context_tokens)
        duration_ms = (time.monotonic() - started) * 1000.0
        return RetrievalResult(
            query=query,
            preset=preset_name,
            chunks=tuple(chunks),
            duration_ms=duration_ms,
            loops=loop,
        )

    @staticmethod
    def _fit_context(
        chunks: list[RetrievedChunk],
        max_context_tokens: int,
    ) -> list[RetrievedChunk]:
        """Keep whole chunks until the token cap, then truncate the next one.

        Why truncate rather than drop? If the cap is hit exactly at a boundary,
        dropping the next chunk could remove the only passage that answers the
        question. Truncating keeps its highest-ranked material and makes the
        cost visible. The truncation is marked in the text so the model does not
        treat an incomplete sentence as complete evidence.
        """
        kept: list[RetrievedChunk] = []
        used = 0
        for chunk in chunks:
            chunk_tokens = estimate_tokens(chunk.text)
            if used + chunk_tokens <= max_context_tokens:
                kept.append(chunk)
                used += chunk_tokens
                continue
            remaining = max_context_tokens - used
            if remaining < 80:
                # Too little room to be useful; stop rather than add a stub.
                break
            # Approximate characters from tokens; the estimator uses 3.5
            # chars/token. Keep a whole sentence boundary if possible.
            character_budget = int(remaining * 3.5)
            truncated = chunk.text[:character_budget].rsplit(" ", 1)[0].strip()
            if truncated:
                kept.append(
                    RetrievedChunk(
                        **{
                            **chunk.__dict__,
                            "text": truncated + " [evidence truncated to fit the token budget]",
                        }
                    )
                )
            break
        return kept

    def expanded_query(self, question: str, chunks: tuple[RetrievedChunk, ...]) -> str:
        """Build a re-retrieval query when the first attempt was insufficient.

        The expansion is conservative: it adds the title/section of the best
        chunk plus a few content terms that were missing from the query. It does
        **not** invent synonyms or call a model. If expansion does not help, the
        evidence gate still refuses; the loop cannot manufacture evidence.
        """
        if not chunks:
            return question
        best = chunks[0]
        additions: list[str] = []
        if best.section:
            additions.append(best.section)
        query_terms = set(content_terms(question))
        chunk_terms = content_terms(best.text)
        for term in chunk_terms:
            if term not in query_terms and len(additions) < 8:
                additions.append(term)
        return f"{question} {' '.join(additions)}".strip()

    def document_body_terms(self, preset_name: str, doc_id: str) -> set[str]:
        """Body vocabulary of one paper, for the evidence presence check."""
        return document_body_terms(self.settings, self.preset(preset_name), doc_id)


def assign_source_ids(chunks: tuple[RetrievedChunk, ...]) -> tuple[RetrievedChunk, ...]:
    """Give each chunk a short citation id ``S1``, ``S2``, ...

    The id is assigned after retrieval and before prompting. The verifier checks
    that every ``[S#]`` in the answer maps back to one of these chunks that were
    actually retrieved. That is the difference between "the paper exists" and
    "this passage supports this sentence".
    """
    return tuple(
        RetrievedChunk(**{**chunk.__dict__, "source_id": f"S{index}"})
        for index, chunk in enumerate(chunks, start=1)
    )
