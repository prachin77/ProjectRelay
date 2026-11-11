"""PDF ingestion: verify, clean, section-detect, chunk.

WHY THIS MODULE IS LONGER THAN IT LOOKS
---------------------------------------
A retrieval system is only as good as its chunks, and a citation is only
credible if its page and section are real. PDF extraction on academic papers is
messy: running headers, page numbers, soft hyphens, ligatures, two-column
layouts. The brief explicitly says not to invent page numbers or citations, so
this module preserves the page number of every chunk and never guesses one.

THE PIPELINE
------------
1. Verify each PDF's SHA-256 against ``config/corpus_manifest.json``.
2. Extract text page by page (``pypdf``) — never merge pages before chunking,
   because a page-bounded chunk has an unambiguous page number.
3. Clean the text:
   - Unicode NFKC (ligatures, full-width characters);
   - typographic quotes/dashes;
   - soft hyphens at line ends;
   - running headers/footers detected by repetition across pages;
   - page-number-only lines.
4. Detect numbered section headings and carry the current section forward.
5. Chunk within a page, with overlap, targeting ``chunk_words``.
6. Emit one JSONL record per chunk per retrieval preset.

WHY PAGE-BOUNDED CHUNKING?
--------------------------
A chunk that spans pages 4–5 makes "page 4 or 5?" a guess. Splitting at page
boundaries costs a little context continuity and buys *verifiable* citations.
That is the right trade for this assignment.

WHY WORD-BASED CHUNKS?
----------------------
Word counts are tokenizer-independent and stable across machines. The actual
prompt cost is measured separately with the local token estimator and replaced
by provider usage after the call.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader

from the_relay.config import (
    PROJECT_ROOT,
    RetrievalConfig,
    Settings,
    load_retrieval_config,
)

# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

# A line that is only a page number, optionally prefixed by "Page".
_PAGE_NUMBER_RE = re.compile(r"^\s*(?:page\s*)?\d{1,4}\s*$", re.IGNORECASE)
# A line that is only an arXiv stamp or a date, common in PDF margins.
_MARGIN_RE = re.compile(
    r"^\s*(?:arxiv:\s*\d{4}\.\d{4,5}(?:v\d+)?(?:\s*\[[^\]]+\])?|"
    r"\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}|\d{4}-\d{2}-\d{2})\s*$",
    re.IGNORECASE,
)
# Numbered headings: "3.2 Attention", "4.1.1 Scaled Dot-Product Attention".
_NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,3})\s+([A-Z][^\n]{2,90})\s*$")
# Common unnumbered headings that appear alone on a line.
_UNNUMBERED_HEADINGS = {
    "abstract",
    "introduction",
    "related work",
    "background",
    "method",
    "methods",
    "approach",
    "experiments",
    "experimental setup",
    "results",
    "discussion",
    "conclusion",
    "conclusions",
    "references",
    "appendix",
    "limitations",
    "acknowledgements",
    "acknowledgments",
}
# Prefixes that usually mean a hyphen at a line break is a real compound, not
# a soft hyphen inserted by the PDF. The heuristic is imperfect; it is
# documented and tested.
_COMPOUND_PREFIXES = {
    "self", "multi", "non", "pre", "post", "co", "re", "de", "anti", "semi",
    "sub", "super", "inter", "intra", "cross", "end", "full", "real", "open",
    "zero", "few", "state", "token", "speech", "text", "end", "high", "low",
    "long", "short", "fine", "coarse", "on", "off", "in", "out", "up", "down",
}


def _normalise_unicode(text: str) -> str:
    """Fold compatibility characters and normalise quotes/dashes.

    NFKC turns ``ﬁ`` into ``fi`` and full-width Latin into ASCII. That matters
    because the same word extracted two ways should tokenise identically.
    """
    text = unicodedata.normalize("NFKC", text)
    translation = {
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
        "\u201c": '"', "\u201d": '"', "\u201e": '"',
        "\u2013": "-", "\u2014": "-", "\u2212": "-",
        "\u00a0": " ",
    }
    return "".join(translation.get(ch, ch) for ch in text)


def _dehyphenate(text: str) -> str:
    """Join words broken across a line by a soft hyphen.

    Example: ``"trans-\nformer"`` -> ``"transformer"``. But ``"speech-\ntext"``
    should stay ``"speech-text"``. We use a small prefix list to decide. This is
    a heuristic, not a language model; it is tested on the actual corpus.
    """
    def join(match: re.Match[str]) -> str:
        left = match.group(1)
        right = match.group(2)
        if left.lower() in _COMPOUND_PREFIXES or len(left) <= 2:
            return f"{left}-{right}"
        return f"{left}{right}"

    # Only joins across a line break, never a hyphen inside a line.
    return re.sub(r"([A-Za-z]+)-\s*\n\s*([A-Za-z]+)", join, text)


def _clean_page_text(raw: str) -> str:
    """Apply the deterministic cleaning steps to one page."""
    text = _normalise_unicode(raw)
    text = _dehyphenate(text)
    # Normalise line endings and collapse runs of spaces, but keep newlines:
    # line structure is needed for heading detection.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _line_key(line: str) -> str:
    """Normalise a line for repetition detection."""
    return re.sub(r"\s+", " ", line).strip().lower()


def detect_running_lines(pages: list[str], *, threshold: float = 0.30) -> set[str]:
    """Return normalized lines repeated on at least ``threshold`` of pages.

    This is how the Moshi running header is removed. The detector is
    conservative: it only considers short lines (<120 characters) and requires
    the line to appear on at least 30% of pages (and at least 3 pages). A real
    sentence repeated that often in a paper is almost certainly a header or a
    footer, not content.
    """
    if len(pages) < 3:
        return set()
    counts: dict[str, int] = {}
    for page in pages:
        seen_on_this_page: set[str] = set()
        for line in page.splitlines():
            key = _line_key(line)
            if not key or len(key) > 120:
                continue
            if _PAGE_NUMBER_RE.match(key) or _MARGIN_RE.match(key):
                continue
            seen_on_this_page.add(key)
        for key in seen_on_this_page:
            counts[key] = counts.get(key, 0) + 1

    minimum_pages = max(3, int(threshold * len(pages)))
    return {line for line, count in counts.items() if count >= minimum_pages}


def _strip_running_lines(text: str, running: set[str]) -> str:
    """Remove running header/footer lines and page-number-only lines."""
    kept: list[str] = []
    for line in text.splitlines():
        key = _line_key(line)
        if key in running:
            continue
        if _PAGE_NUMBER_RE.match(key) or _MARGIN_RE.match(key):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


# ---------------------------------------------------------------------------
# Section detection and paragraph splitting
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SectionSpan:
    """A span of a page that belongs to one section heading."""

    section: str | None
    text: str


def split_into_sections(page_text: str, current_section: str | None) -> tuple[list[SectionSpan], str]:
    """Split a page into section spans and return the last section seen.

    The function walks line by line. When it recognises a heading, it closes the
    previous span and starts a new one. The heading text is kept in the span
    (it is useful evidence), but the section label is metadata, not a guess:
    it comes from a heading actually present in the page.
    """
    lines = page_text.splitlines()
    spans: list[SectionSpan] = []
    buffer: list[str] = []
    section = current_section

    def flush() -> None:
        nonlocal buffer
        body = "\n".join(buffer).strip()
        if body:
            spans.append(SectionSpan(section=section, text=body))
        buffer = []

    for line in lines:
        stripped = line.strip()
        numbered = _NUMBERED_HEADING_RE.match(stripped)
        unnumbered = _line_key(stripped) in _UNNUMBERED_HEADINGS
        is_heading = bool(numbered or unnumbered) and len(stripped) <= 100
        if is_heading:
            flush()
            if numbered:
                section = f"{numbered.group(1)} {numbered.group(2).strip()}"
            else:
                section = stripped
            buffer.append(stripped)
        else:
            buffer.append(line)
    flush()
    return spans, section


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])")
_WORD_RE = re.compile(r"\S+")


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def _sentences(text: str) -> list[str]:
    """Split a paragraph into sentences without breaking common abbreviations.

    This is a heuristic. It only needs to be good enough to keep chunks
    readable and to give the verifier claim-sized units.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    sentences = _SENTENCE_SPLIT_RE.split(text)
    # Re-join fragments that are almost certainly abbreviations.
    merged: list[str] = []
    for sentence in sentences:
        if merged and re.search(r"\b(?:e\.g|i\.e|et al|Fig|Eq|vs|Dr|Mr|Ms)\.$", merged[-1]):
            merged[-1] = f"{merged[-1]} {sentence}"
        else:
            merged.append(sentence)
    return [s for s in merged if s.strip()]


def chunk_text(
    text: str,
    *,
    chunk_words: int,
    overlap_words: int,
) -> list[str]:
    """Split ``text`` into overlapping chunks of roughly ``chunk_words`` words.

    The splitter works on sentences, so a chunk never ends mid-sentence unless a
    single sentence is longer than the target (then it is split on words). This
    makes chunks readable and makes a citation point at a coherent passage.
    """
    if chunk_words <= 0:
        raise ValueError("chunk_words must be positive")
    if overlap_words < 0 or overlap_words >= chunk_words:
        raise ValueError("overlap_words must be in [0, chunk_words)")

    sentences = _sentences(text)
    if not sentences:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_words = 0

    def emit() -> None:
        nonlocal current, current_words
        if current:
            chunks.append(" ".join(current).strip())
        # Keep the last few sentences as overlap for the next chunk.
        if overlap_words <= 0:
            current = []
            current_words = 0
            return
        kept: list[str] = []
        kept_words = 0
        for sentence in reversed(current):
            sentence_words = _word_count(sentence)
            if kept_words + sentence_words > overlap_words:
                break
            kept.insert(0, sentence)
            kept_words += sentence_words
        current = kept
        current_words = kept_words

    for sentence in sentences:
        sentence_words = _word_count(sentence)
        # A pathological single sentence longer than the target is split on
        # words so the chunk size bound is still respected.
        if sentence_words > chunk_words:
            if current:
                emit()
            words = _WORD_RE.findall(sentence)
            for start in range(0, len(words), chunk_words):
                chunks.append(" ".join(words[start:start + chunk_words]))
            current = []
            current_words = 0
            continue
        if current_words + sentence_words > chunk_words and current:
            emit()
        current.append(sentence)
        current_words += sentence_words

    if current:
        chunks.append(" ".join(current).strip())

    # Merge a tiny tail into the previous chunk so we do not create a chunk with
    # almost no evidence. It is better to slightly exceed the target than to
    # create a citation that says nothing.
    if len(chunks) >= 2 and _word_count(chunks[-1]) < max(20, chunk_words // 5):
        tail = chunks.pop()
        chunks[-1] = f"{chunks[-1]} {tail}".strip()
    return chunks


# ---------------------------------------------------------------------------
# Corpus loading and verification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Paper:
    doc_id: str
    title: str
    arxiv_id: str
    version: str
    version_date: str
    url: str
    file: Path
    sha256: str
    bytes: int
    pages: int


def load_manifest(settings: Settings) -> list[Paper]:
    """Load and validate ``config/corpus_manifest.json``."""
    path = settings.resolved(settings.config_dir) / "corpus_manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    papers: list[Paper] = []
    for item in data["papers"]:
        papers.append(
            Paper(
                doc_id=item["doc_id"],
                title=item["title"],
                arxiv_id=item["arxiv_id"],
                version=item["version"],
                version_date=item["version_date"],
                url=item["url"],
                file=settings.resolved(Path(item["file"])),
                sha256=item["sha256"],
                bytes=int(item["bytes"]),
                pages=int(item["pages"]),
            )
        )
    return papers


def verify_corpus(settings: Settings) -> list[dict[str, object]]:
    """Verify every PDF exists and matches its recorded SHA-256.

    Returns a list of per-paper result dicts. A mismatch is a hard error for
    ingestion: we never silently index a different document under the same
    identity.
    """
    results: list[dict[str, object]] = []
    for paper in load_manifest(settings):
        if not paper.file.exists():
            results.append(
                {
                    "doc_id": paper.doc_id,
                    "ok": False,
                    "reason": f"missing file: {paper.file}",
                }
            )
            continue
        digest = hashlib.sha256(paper.file.read_bytes()).hexdigest()
        results.append(
            {
                "doc_id": paper.doc_id,
                "ok": digest == paper.sha256,
                "reason": "hash matches" if digest == paper.sha256 else "sha256 mismatch",
                "expected": paper.sha256,
                "actual": digest,
                "path": str(paper.file),
            }
        )
    return results


def corpus_version(settings: Settings, preset: object) -> str:
    """Stable identity of the corpus + chunking configuration.

    Part of every cache key. If a PDF or a chunking parameter changes, old
    answers cannot be served by accident.
    """
    papers = load_manifest(settings)
    payload = {
        "pdfs": sorted((p.doc_id, p.sha256) for p in papers),
        "preset": getattr(preset, "as_dict", str(preset)),
        "chunker": "page-bounded-sentence-v1",
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Main ingestion
# ---------------------------------------------------------------------------

def extract_pages(paper: Paper) -> list[str]:
    """Extract each page's cleaned text, preserving page boundaries."""
    reader = PdfReader(str(paper.file))
    raw_pages = [page.extract_text() or "" for page in reader.pages]
    cleaned = [_clean_page_text(text) for text in raw_pages]
    running = detect_running_lines(cleaned)
    return [_strip_running_lines(text, running) for text in cleaned]


def build_chunks_for_preset(
    settings: Settings,
    paper: Paper,
    *,
    preset_name: str,
    chunk_words: int,
    overlap_words: int,
) -> list[dict[str, object]]:
    """Build all chunks for one paper under one retrieval preset.

    Each chunk carries: document identity, title, arXiv id/version, page,
    section, chunk id, text, and the chunking configuration. No field is
    invented: ``page`` is the real page index (1-based), ``section`` is the
    last heading actually seen on that page (or None).
    """
    pages = extract_pages(paper)
    records: list[dict[str, object]] = []
    current_section: str | None = None

    for page_index, page_text in enumerate(pages, start=1):
        if not page_text.strip():
            continue
        spans, current_section = split_into_sections(page_text, current_section)
        chunk_index = 0
        for span in spans:
            for piece in chunk_text(
                span.text,
                chunk_words=chunk_words,
                overlap_words=overlap_words,
            ):
                # A page that contains only a heading ("References") produces a
                # 1-2 word "chunk". It cannot support a claim, and it pollutes
                # the index with low-information passages, so drop it. The
                # threshold is deliberately low (12 words); real evidence is
                # never that short.
                if _word_count(piece) < 12:
                    continue
                records.append(
                    {
                        "chunk_id": f"{paper.doc_id}:p{page_index}:c{chunk_index}",
                        "doc_id": paper.doc_id,
                        "title": paper.title,
                        "arxiv_id": paper.arxiv_id,
                        "version": paper.version,
                        "page": page_index,
                        "section": span.section,
                        "text": piece,
                        "words": _word_count(piece),
                        "preset": preset_name,
                        "chunk_words_target": chunk_words,
                        "overlap_words": overlap_words,
                        "source_url": paper.url,
                    }
                )
                chunk_index += 1
    return records


def ingest_corpus(
    settings: Settings,
    *,
    retrieval_config: RetrievalConfig | None = None,
    presets: Iterable[str] | None = None,
) -> dict[str, object]:
    """Verify the corpus and write one chunk file per retrieval preset.

    Returns a small statistics dictionary that is printed by the CLI and
    recorded in ``indexes/corpus_stats.json``.
    """
    config = retrieval_config or load_retrieval_config(settings)
    selected = list(presets) if presets is not None else list(config.presets)
    verification = verify_corpus(settings)
    failures = [r for r in verification if not r["ok"]]
    if failures:
        raise ValueError(f"corpus verification failed: {failures}")

    papers = load_manifest(settings)
    stats: dict[str, object] = {
        "verified": verification,
        "presets": {},
    }
    index_dir = settings.resolved(settings.index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)

    for preset_name in selected:
        preset = config.get(preset_name)
        all_chunks: list[dict[str, object]] = []
        per_doc: dict[str, int] = {}
        for paper in papers:
            chunks = build_chunks_for_preset(
                settings,
                paper,
                preset_name=preset_name,
                chunk_words=preset.chunk_words,
                overlap_words=preset.overlap_words,
            )
            all_chunks.extend(chunks)
            per_doc[paper.doc_id] = len(chunks)

        out_path = index_dir / f"chunks_{preset_name}.jsonl"
        with out_path.open("w", encoding="utf-8") as fh:
            for record in all_chunks:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        word_counts = [int(c["words"]) for c in all_chunks]
        stats["presets"][preset_name] = {
            "chunks": len(all_chunks),
            "per_doc": per_doc,
            "mean_words": round(sum(word_counts) / len(word_counts), 1) if word_counts else 0,
            "min_words": min(word_counts) if word_counts else 0,
            "max_words": max(word_counts) if word_counts else 0,
            "chunk_words_target": preset.chunk_words,
            "overlap_words": preset.overlap_words,
            "top_k": preset.top_k,
            "max_context_tokens": preset.max_context_tokens,
            "fingerprint": preset.fingerprint,
            "corpus_version": corpus_version(settings, preset),
            "file": str(out_path),
        }

    stats_path = index_dir / "corpus_stats.json"
    stats_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats


def load_chunks(settings: Settings, preset_name: str) -> list[dict[str, object]]:
    """Load the chunk file for a preset, failing loudly if it is missing."""
    path = settings.resolved(settings.index_dir) / f"chunks_{preset_name}.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"chunk file not found: {path}. Run `py -m the_relay.main ingest` first."
        )
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def main() -> None:  # pragma: no cover - convenience entrypoint
    """Allow ``python -m the_relay.rag.ingest`` as a shortcut."""
    from the_relay.config import get_settings

    settings = get_settings()
    stats = ingest_corpus(settings)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
