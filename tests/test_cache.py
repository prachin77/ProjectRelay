"""Exact cache behaviour, including the version-aware key.

The cache must never serve an answer produced under a different corpus, prompt,
model or retrieval preset. That is what makes an exact cache safe where a
semantic cache is not.
"""

from __future__ import annotations

import time

from the_relay.core.cache import CacheEntry, ExactCache


def _entry(**overrides) -> CacheEntry:
    values = {
        "answer": "A grounded answer [S1].",
        "citation_ids": ("S1",),
        "citation_metadata": ({"source_id": "S1", "page": 4},),
        "evidence_level": "sufficient",
        "verification_passed": True,
        "created_at": time.monotonic(),
        "expires_at": time.monotonic() + 60,
        "preset": "balanced",
        "model_id": "standin/deterministic-extractive-v1",
        "prompt_version": "p1",
        "corpus_version": "v1",
    }
    values.update(overrides)
    return CacheEntry(**values)


def _key(**overrides) -> str:
    values = {
        "preset": "balanced",
        "model_id": "standin/deterministic-extractive-v1",
        "prompt_version": "p1",
        "corpus_version": "v1",
        "temperature": 0.0,
        "max_output_tokens": 400,
        "source": "stand-in",
    }
    values.update(overrides)
    return ExactCache.make_key("how large is the dataset?", **values)


def test_miss_then_hit() -> None:
    cache = ExactCache()
    key = _key()
    assert cache.get(key) is None
    cache.put(key, _entry())
    assert cache.get(key) is not None
    assert cache.stats.hits == 1
    assert cache.stats.misses == 1


def test_key_changes_with_every_meaningful_setting() -> None:
    base = _key()
    assert _key(preset="extensive") != base
    assert _key(model_id="other-model") != base
    assert _key(prompt_version="p2") != base
    assert _key(corpus_version="v2") != base
    assert _key(temperature=1.0) != base
    assert _key(max_output_tokens=200) != base
    assert _key(source="real") != base


def test_key_is_stable_for_the_same_inputs() -> None:
    assert _key() == _key()


def test_disabled_cache_always_misses() -> None:
    cache = ExactCache(enabled=False)
    key = _key()
    cache.put(key, _entry())
    assert cache.get(key) is None
    assert len(cache) == 0


def test_expired_entry_is_a_miss() -> None:
    cache = ExactCache()
    key = _key()
    cache.put(key, _entry(expires_at=time.monotonic() - 1))
    assert cache.get(key) is None
    assert cache.stats.expired == 1


def test_oldest_entry_is_evicted_at_capacity() -> None:
    cache = ExactCache(max_entries=2)
    first, second, third = _key(preset="light"), _key(preset="balanced"), _key(
        preset="extensive"
    )
    cache.put(first, _entry())
    cache.put(second, _entry())
    cache.put(third, _entry())
    assert len(cache) == 2
    assert cache.get(first) is None
    assert cache.stats.evictions == 1
