"""Local hybrid retrieval: BM25 + TF-IDF + Reciprocal Rank Fusion + rerank.

WHY HYBRID?
-----------
These papers are full of exact technical tokens: ``QK^T``, ``Mimi``, ``H-Net``,
``RQD``, ``FSQ``, ``BLEU``. A lexical retriever (BM25) is excellent at those and
at number matching. A TF-IDF cosine retriever handles some paraphrase and
down-weights words that occur everywhere. Neither is sufficient alone, so we
fuse their rankings.

WHY RRF AND NOT A WEIGHTED SUM?
-------------------------------
BM25 scores and cosine similarities live on different scales. Adding them
requires an arbitrary weight and a normalisation that changes whenever the
corpus changes. Reciprocal Rank Fusion uses only the *order* each retriever
produced, so it is scale-free. The constant ``rrf_k`` (60) comes from the
original RRF paper and damps the influence of any single top hit.

WHAT THE RERANKER IS (AND IS NOT)
---------------------------------
It is a deterministic, explainable scorer: query-term coverage, bigram overlap,
number match, and title/section match. It is **not** a neural cross-encoder. It
costs no API tokens and no model download. It is weaker than a cross-encoder on
paraphrase, which is recorded as a limitation and a candidate improvement.

COST
----
Zero provider tokens: everything here runs locally. Latency is milliseconds for
a 120-page corpus, which is why retrieval can be re-run in a bounded loop
without touching the quota.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from rank_bm25 import BM25Okapi

from the_relay.config import RetrievalConfig, RetrievalPreset, Settings
from the_relay.rag.ingest import corpus_version, load_chunks
from the_relay.types import RetrievedChunk

# A deliberately small stopword list. We keep short but meaningful tokens such
# as "q", "k", "v" because the Transformer paper uses them as symbols.
_STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those of in on at to for
    from by with without into about over under between among during before
    after above below up down out off again further once here there when where
    why how all any both each few more most other some such no nor not only own
    same so too very can will just should now is are was were be been being do
    does did doing have has had having i you he she it we they me him her us
    them my your his its our their what which who whom whose
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.\-^][a-z0-9]+)*")
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)*\s*(?:%|k|m|bn|ms|s)?\b", re.IGNORECASE)
# A number with a unit is what answers "how large / how many / what latency".
# The unit list is deliberately small and literal; it is a ranking hint, not a
# measurement parser.
_NUMBER_UNIT_RE = re.compile(
    r"\b\d+(?:[.,]\d+)*\s*(?:%|k|m|bn|ms|s|hours?|days?|minutes?|gpus?|"
    r"million|billion|thousand|parameters?|layers?|steps?)\b",
    re.IGNORECASE,
)
_SIZE_QUESTION_RE = re.compile(
    r"\b(?:how large|how many|how much|what percentage|what fraction|"
    r"what latency|what score|what bleu|what wer|number of)\b",
    re.IGNORECASE,
)
# Back-matter passages (reference lists, acknowledgements) are part of the PDF
# but are not evidence for a technical claim. In the balanced index they are
# roughly a third of all chunks, so a naive retriever spends a large share of
# its prompt budget quoting bibliography entries. They are filtered before the
# final top-k; the count remains visible in the index metadata.
_BACK_MATTER_RE = re.compile(r"^\s*(?:references|bibliography|acknowledg)", re.IGNORECASE)


def _is_back_matter(chunk: dict[str, object]) -> bool:
    return bool(_BACK_MATTER_RE.match(str(chunk.get("section") or "")))


def tokenize(text: str) -> list[str]:
    """Tokenise text for BM25/TF-IDF.

    Keeps dots, hyphens and carets inside tokens so ``QK^T``, ``H-Net`` and
    ``3.5`` survive. Lower-cases everything: retrieval is case-insensitive.
    """
    return [
        token
        for token in _TOKEN_RE.findall(text.lower())
        if token not in _STOPWORDS and len(token) >= 1
    ]


def content_terms(text: str) -> list[str]:
    """Unique content terms, order preserved (for coverage calculations)."""
    seen: set[str] = set()
    out: list[str] = []
    for token in tokenize(text):
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _l2_normalise(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


@dataclass
class _IndexData:
    """In-memory index for one retrieval preset."""

    chunks: list[dict[str, object]]
    tokenized: list[list[str]]
    bm25: BM25Okapi
    vocabulary: dict[str, int]
    idf: np.ndarray
    tfidf_matrix: np.ndarray
    chunk_ids: list[str]
    chunk_id_to_position: dict[str, int]
    corpus_fingerprint: str
    preset_name: str


# Module-level cache: building a 400-chunk BM25 + TF-IDF index is fast, but we
# do not want to rebuild it for every question in an evaluation run.
_INDEX_CACHE: dict[str, _IndexData] = {}


def _build_index(
    settings: Settings,
    preset: RetrievalPreset,
) -> _IndexData:
    raw_chunks = load_chunks(settings, preset.name)
    if not raw_chunks:
        raise ValueError(
            f"no chunks for preset {preset.name!r}; run ingestion first"
        )

    tokenized = [tokenize(str(chunk["text"])) for chunk in raw_chunks]
    # BM25Okapi requires at least one document and at least one token. A page
    # consisting only of a heading can tokenise to [] — keep it out of the
    # lexical index but keep it available for retrieval if TF-IDF finds it.
    lexical_docs = [tokens if tokens else ["__empty__"] for tokens in tokenized]
    bm25 = BM25Okapi(lexical_docs)

    # Build a TF-IDF matrix from the same tokenisation. min_df=1 because the
    # corpus is small; a term appearing in one chunk may be the decisive term.
    vocabulary: dict[str, int] = {}
    document_frequency: dict[str, int] = {}
    for tokens in tokenized:
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    for token in sorted(document_frequency):
        vocabulary[token] = len(vocabulary)

    n_documents = len(tokenized)
    idf = np.zeros(len(vocabulary), dtype=np.float64)
    for token, index in vocabulary.items():
        df = document_frequency[token]
        # Smoothed IDF; always positive, so a rare term cannot get a negative
        # weight and flip a ranking.
        idf[index] = math.log((1.0 + n_documents) / (1.0 + df)) + 1.0

    matrix = np.zeros((n_documents, len(vocabulary)), dtype=np.float64)
    for row, tokens in enumerate(tokenized):
        if not tokens:
            continue
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        for token, count in counts.items():
            column = vocabulary[token]
            # Sublinear term frequency: a word repeated 20 times is not 20x
            # more important than a word repeated once.
            matrix[row, column] = (1.0 + math.log(count)) * idf[column]
    matrix = _l2_normalise(matrix)

    chunk_ids = [str(chunk["chunk_id"]) for chunk in raw_chunks]
    return _IndexData(
        chunks=raw_chunks,
        tokenized=tokenized,
        bm25=bm25,
        vocabulary=vocabulary,
        idf=idf,
        tfidf_matrix=matrix,
        chunk_ids=chunk_ids,
        chunk_id_to_position={chunk_id: i for i, chunk_id in enumerate(chunk_ids)},
        corpus_fingerprint=corpus_version(settings, preset),
        preset_name=preset.name,
    )


def get_index(settings: Settings, preset: RetrievalPreset) -> _IndexData:
    """Return a cached index, rebuilding if the corpus fingerprint changed."""
    cache_key = f"{preset.name}:{preset.fingerprint}"
    cached = _INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached
    index = _build_index(settings, preset)
    _INDEX_CACHE[cache_key] = index
    return index


def _bm25_ranking(index: _IndexData, query: str) -> list[tuple[int, float]]:
    query_tokens = tokenize(query)
    if not query_tokens:
        return []
    scores = index.bm25.get_scores(query_tokens)
    ranked = sorted(
        ((i, float(score)) for i, score in enumerate(scores)),
        key=lambda pair: pair[1],
        reverse=True,
    )
    # BM25 returns 0 for every document when no query term occurs. Those are
    # not "weak matches"; they are no match, and including them would add noise.
    return [(i, score) for i, score in ranked if score > 0.0]


def _tfidf_ranking(index: _IndexData, query: str) -> list[tuple[int, float]]:
    query_tokens = tokenize(query)
    if not query_tokens:
        return []
    vector = np.zeros(len(index.vocabulary), dtype=np.float64)
    counts: dict[str, int] = {}
    for token in query_tokens:
        if token in index.vocabulary:
            counts[token] = counts.get(token, 0) + 1
    if not counts:
        return []
    for token, count in counts.items():
        column = index.vocabulary[token]
        vector[column] = (1.0 + math.log(count)) * index.idf[column]
    norm = np.linalg.norm(vector)
    if norm == 0:
        return []
    vector /= norm
    scores = index.tfidf_matrix @ vector
    ranked = sorted(
        ((i, float(score)) for i, score in enumerate(scores)),
        key=lambda pair: pair[1],
        reverse=True,
    )
    return [(i, score) for i, score in ranked if score > 0.0]


def _reciprocal_rank_fusion(
    rankings: Iterable[list[tuple[int, float]]],
    *,
    rrf_k: int,
    pool_size: int,
) -> list[tuple[int, float]]:
    """Fuse ranked lists by RRF and return the top ``pool_size``."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, (position, _score) in enumerate(ranking, start=1):
            fused[position] = fused.get(position, 0.0) + 1.0 / (rrf_k + rank)
    ranked = sorted(fused.items(), key=lambda pair: pair[1], reverse=True)
    return ranked[:pool_size]


def _query_bigrams(query: str) -> set[tuple[str, str]]:
    tokens = tokenize(query)
    return set(zip(tokens, tokens[1:]))


def _relevance(
    index: _IndexData,
    position: int,
    query: str,
    query_terms: list[str],
    query_bigrams: set[tuple[str, str]],
) -> tuple[float, float, float, float, float]:
    """Return a deterministic 0..1 relevance plus its components.

    Components:
    - coverage: fraction of unique query terms present in the chunk;
    - bigram: fraction of query bigrams present;
    - numeric: 1.0 if every number in the query appears in the chunk;
    - title: overlap between query terms and the paper title;
    - combined: weighted sum, clamped to 0..1.
    """
    chunk = index.chunks[position]
    text = str(chunk["text"])
    chunk_tokens = set(index.tokenized[position])
    chunk_token_list = index.tokenized[position]

    coverage = (
        sum(1 for term in query_terms if term in chunk_tokens) / len(query_terms)
        if query_terms
        else 0.0
    )

    if query_bigrams:
        chunk_bigrams = set(zip(chunk_token_list, chunk_token_list[1:]))
        bigram = len(query_bigrams & chunk_bigrams) / len(query_bigrams)
    else:
        bigram = 0.0

    query_numbers = {m.group(0).strip().lower() for m in _NUMBER_RE.finditer(query)}
    chunk_numbers = {m.group(0).strip().lower() for m in _NUMBER_RE.finditer(text)}
    numeric = 1.0 if query_numbers and query_numbers <= chunk_numbers else 0.0
    if numeric == 0.0 and _SIZE_QUESTION_RE.search(query) and _NUMBER_UNIT_RE.search(text):
        # The question asks for a quantity and this passage contains a number
        # with a unit. That is weak evidence of usefulness, not proof, so it
        # only influences the reranker -- never the evidence gate.
        numeric = 1.0

    title_terms = set(content_terms(str(chunk.get("title", ""))))
    title = (
        sum(1 for term in query_terms if term in title_terms) / len(query_terms)
        if query_terms
        else 0.0
    )

    combined = 0.55 * coverage + 0.25 * bigram + 0.10 * numeric + 0.10 * title
    return min(1.0, max(0.0, combined)), coverage, bigram, numeric, title


def search(
    settings: Settings,
    preset: RetrievalPreset,
    query: str,
    *,
    fusion_pool: int,
    rrf_k: int,
    top_k_override: int | None = None,
    doc_filter: str | None = None,
) -> list[RetrievedChunk]:
    """Retrieve the top passages for ``query`` under one preset.

    Parameters
    ----------
    settings:
        Used to locate the index and to compute the corpus fingerprint.
    preset:
        Chunking + top-k + reranking configuration.
    query:
        The natural-language question.
    fusion_pool:
        How many candidates each retriever contributes before fusion.
    rrf_k:
        RRF damping constant.
    top_k_override:
        Used by the evidence loop to fetch more candidates without changing
        the preset. Does not affect the prompt cap (that is enforced later).
    doc_filter:
        If set, only chunks from this document are eligible. Applied *before*
        fusion so the candidate pool is not consumed by the other papers.

    Returns
    -------
    A list of :class:`RetrievedChunk`, ordered by the final ranking.
    """
    index = get_index(settings, preset)
    allowed: set[int] | None = None
    if doc_filter is not None:
        allowed = {
            position
            for position, chunk in enumerate(index.chunks)
            if str(chunk["doc_id"]) == doc_filter
        }
        if not allowed:
            return []

    bm25_ranking = _bm25_ranking(index, query)
    tfidf_ranking = _tfidf_ranking(index, query)
    if allowed is not None:
        bm25_ranking = [item for item in bm25_ranking if item[0] in allowed]
        tfidf_ranking = [item for item in tfidf_ranking if item[0] in allowed]
    fused = _reciprocal_rank_fusion(
        [bm25_ranking, tfidf_ranking],
        rrf_k=rrf_k,
        pool_size=fusion_pool,
    )
    if not fused:
        return []

    limit = top_k_override if top_k_override is not None else preset.top_k
    # Drop bibliography/acknowledgement passages, but never below the number of
    # results the caller asked for: on a query that only matches back matter,
    # returning something is better than returning nothing.
    without_back_matter = [
        item for item in fused if not _is_back_matter(index.chunks[item[0]])
    ]
    if len(without_back_matter) >= max(1, limit):
        fused = without_back_matter

    query_terms = content_terms(query)
    query_bigrams = _query_bigrams(query)
    scored: list[tuple[int, float, float, float, float, float, float]] = []
    for position, fused_score in fused:
        relevance, coverage, bigram, numeric, title = _relevance(
            index, position, query, query_terms, query_bigrams
        )
        scored.append(
            (position, fused_score, relevance, coverage, bigram, numeric, title)
        )

    if preset.rerank:
        # Quality setting: order by the deterministic reranker, using fused
        # score only as a tie-breaker.
        scored.sort(key=lambda row: (row[2], row[1]), reverse=True)
    else:
        # Lightweight setting: keep the fusion order; still compute relevance
        # because the evidence gate needs it.
        scored.sort(key=lambda row: row[1], reverse=True)

    limit = top_k_override if top_k_override is not None else preset.top_k
    selected = scored[: max(1, limit)]
    results: list[RetrievedChunk] = []
    for rank, (position, fused_score, relevance, _cov, _bi, _num, _title) in enumerate(
        selected, start=1
    ):
        chunk = index.chunks[position]
        results.append(
            RetrievedChunk(
                chunk_id=str(chunk["chunk_id"]),
                doc_id=str(chunk["doc_id"]),
                title=str(chunk["title"]),
                arxiv_id=str(chunk["arxiv_id"]),
                version=str(chunk["version"]),
                page=int(chunk["page"]) if chunk.get("page") is not None else None,
                section=str(chunk["section"]) if chunk.get("section") else None,
                text=str(chunk["text"]),
                fused_score=float(fused_score),
                relevance=float(relevance),
                rank=rank,
            )
        )
    return results


def index_metadata(settings: Settings, preset: RetrievalPreset) -> dict[str, object]:
    """Small summary used in traces and reports (no chunk text)."""
    index = get_index(settings, preset)
    return {
        "preset": preset.name,
        "chunks": len(index.chunks),
        "corpus_fingerprint": index.corpus_fingerprint,
        "vocabulary_size": len(index.vocabulary),
        "top_k": preset.top_k,
        "chunk_words": preset.chunk_words,
        "rerank": preset.rerank,
    }


def document_body_terms(
    settings: Settings,
    preset: RetrievalPreset,
    doc_id: str,
) -> set[str]:
    """Tokenised body vocabulary of one paper, excluding back matter.

    Used by the evidence gate for a *presence* check, not for ranking. Filtering
    references matters: "ImageNet" occurs in the Dynamic Chunking bibliography
    but the paper reports no ImageNet experiments, so counting a reference
    would make the system think it had evidence it does not have.
    """
    index = get_index(settings, preset)
    terms: set[str] = set()
    for position, chunk in enumerate(index.chunks):
        if str(chunk["doc_id"]) != doc_id:
            continue
        if _is_back_matter(chunk):
            continue
        terms.update(index.tokenized[position])
    return terms
