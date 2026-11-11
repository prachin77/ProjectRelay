"""Safe exact answer cache (in-process).

WHY CACHE?
----------
On the documented free tier a single answer can consume 20-35% of one minute's
8,000-token budget. A repeated question served from cache costs zero tokens. It
is the highest-leverage safe cost optimisation in the system.

WHY EXACT, NOT SEMANTIC?
------------------------
Semantic similarity is not equivalence. "Does Moshi use a Transformer?" and
"Does Moshi not use a Transformer?" are nearly identical as vectors and opposite
in meaning. A false semantic hit serves a confidently wrong answer instantly —
the worst failure mode in the whole system. The exact cache is safe because the
key is the deterministic normalised question plus every setting that could
change the answer. Semantic caching is deliberately not built (ADR-009).

WHY IN-PROCESS?
---------------
Redis is not installed on this machine, and the brief does not require a
distributed cache. An in-process cache is honest about its scope: it is lost on
restart and is not shared between workers. That limitation is documented. The
class has the same shape a Redis adapter would need, so it can be replaced later
without touching the pipeline.

FAIL-OPEN
---------
A cache problem must never break an answer. ``get`` returns ``None`` on a miss;
writes are best-effort. The PDFs are the source of truth; the cache is only a
performance layer.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CacheEntry:
    """One cached answer plus everything needed to serve it safely."""

    answer: str
    citation_ids: tuple[str, ...]
    citation_metadata: tuple[dict[str, Any], ...]
    evidence_level: str
    verification_passed: bool
    created_at: float
    expires_at: float
    preset: str
    model_id: str
    prompt_version: str
    corpus_version: str


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    puts: int = 0
    evictions: int = 0
    expired: int = 0


class ExactCache:
    """Small TTL + LRU-ish exact cache.

    ``dict`` preserves insertion order, so evicting the first key is a simple
    FIFO approximation of LRU. That is good enough for a single-process cache;
    it is not claimed to be true LRU.
    """

    def __init__(self, *, enabled: bool = True, ttl_s: int = 86_400, max_entries: int = 512) -> None:
        self.enabled = enabled
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._entries: dict[str, CacheEntry] = {}
        self.stats = CacheStats()

    @staticmethod
    def make_key(
        normalized_question: str,
        *,
        preset: str,
        model_id: str,
        prompt_version: str,
        corpus_version: str,
        temperature: float,
        max_output_tokens: int,
        source: str,
    ) -> str:
        """Build a version-aware key.

        Every component that can change the answer is included. If the corpus,
        chunking, prompt, model, provider, or generation parameters change, old
        entries cannot be served by accident.
        """
        payload = {
            "question": normalized_question,
            "preset": preset,
            "model_id": model_id,
            "prompt_version": prompt_version,
            "corpus_version": corpus_version,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "source": source,
        }
        raw = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> CacheEntry | None:
        if not self.enabled:
            self.stats.misses += 1
            return None
        entry = self._entries.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        if entry.expires_at <= time.monotonic():
            self._entries.pop(key, None)
            self.stats.expired += 1
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return entry

    def put(self, key: str, entry: CacheEntry) -> None:
        if not self.enabled:
            return
        if len(self._entries) >= self.max_entries:
            # FIFO eviction. See class docstring: this is not true LRU.
            oldest = next(iter(self._entries))
            self._entries.pop(oldest, None)
            self.stats.evictions += 1
        self._entries[key] = entry
        self.stats.puts += 1

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def to_dict(self) -> dict[str, int]:
        return {
            "entries": len(self._entries),
            "hits": self.stats.hits,
            "misses": self.stats.misses,
            "puts": self.stats.puts,
            "evictions": self.stats.evictions,
            "expired": self.stats.expired,
        }
