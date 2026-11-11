"""Shared test fixtures and small builders.

WHY THIS FILE EXISTS
--------------------
Every test needs realistic objects without spending tokens or touching the
network. The fixtures below build typed settings, chunks and evaluation
questions in one place, so a schema change breaks one helper instead of a dozen
tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from the_relay.config import PROJECT_ROOT, Settings
from the_relay.eval.dataset import EvalQuestion
from the_relay.types import RetrievedChunk


@pytest.fixture(scope="session")
def project_root() -> Path:
    """The repository root (the PDFs and indexes live here)."""
    return PROJECT_ROOT


@pytest.fixture
def settings_factory(tmp_path: Path):
    """Build isolated settings for one test.

    Quota is deliberately huge by default: these tests exercise one concern at
    a time, and a quota rejection would look like a logic bug. Tests that *want*
    quota pressure override the limits explicitly.
    """

    def _make(**overrides) -> Settings:
        values = {
            "provider": "standin",
            "logs_dir": tmp_path / "logs",
            "results_dir": tmp_path / "results",
            "cache_enabled": False,
            "quota_rpm": 1_000,
            "quota_tpm": 10_000_000,
            "quota_rpd": 100_000,
            "quota_tpd": 100_000_000,
            "retry_jitter": False,
        }
        values.update(overrides)
        return Settings(**values)

    return _make


@pytest.fixture
def make_chunk():
    """Build a ``RetrievedChunk`` with citation metadata filled in."""

    def _make(
        text: str,
        *,
        chunk_id: str = "attention:p4:c1",
        doc_id: str = "attention",
        title: str = "Attention Is All You Need",
        arxiv_id: str = "1706.03762",
        version: str = "v7",
        page: int | None = 4,
        section: str | None = "3.2.1 Scaled Dot-Product Attention",
        relevance: float = 0.8,
        source_id: str = "S1",
    ) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=chunk_id,
            doc_id=doc_id,
            title=title,
            arxiv_id=arxiv_id,
            version=version,
            page=page,
            section=section,
            text=text,
            relevance=relevance,
            source_id=source_id,
        )

    return _make


@pytest.fixture
def make_question():
    """Build an ``EvalQuestion`` without reading the JSON file."""

    def _make(
        *,
        qid: str = "T01",
        question: str = "What BLEU score did the big Transformer achieve?",
        answerable: bool = True,
        required_all: tuple[tuple[str, ...], ...] = (("28.4",), ("bleu",)),
        paper: str = "attention",
        notes: str = "",
    ) -> EvalQuestion:
        return EvalQuestion(
            qid=qid,
            paper=paper,
            question=question,
            answerable=answerable,
            required_all=required_all,
            notes=notes,
        )

    return _make
