"""Spec cache bounds: TTL, LRU eviction, size cap, no fetch-per-sync."""

import pytest

from airbyte_config_drift.spec_cache import SpecCache, SpecTooLargeError


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make_fetcher():
    calls = []

    def fetch(repo, tag):
        calls.append((repo, tag))
        return {"type": "object", "properties": {"tag": {"const": tag}}}

    return fetch, calls


def test_cache_hit_avoids_refetch_within_ttl():
    fetch, calls = make_fetcher()
    cache = SpecCache(fetcher=fetch, ttl_seconds=60)
    cache.get_or_fetch("repo", "1.0")
    cache.get_or_fetch("repo", "1.0")
    cache.get_or_fetch("repo", "1.0")
    assert calls == [("repo", "1.0")]
    assert cache.hits == 2 and cache.misses == 1


def test_expired_entry_is_refetched():
    clock = Clock()
    fetch, calls = make_fetcher()
    cache = SpecCache(fetcher=fetch, ttl_seconds=60, clock=clock)
    cache.get_or_fetch("repo", "1.0")
    clock.now = 61.0
    cache.get_or_fetch("repo", "1.0")
    assert len(calls) == 2


def test_different_versions_cached_separately():
    fetch, calls = make_fetcher()
    cache = SpecCache(fetcher=fetch)
    cache.get_or_fetch("repo", "1.0")
    cache.get_or_fetch("repo", "2.0")
    assert calls == [("repo", "1.0"), ("repo", "2.0")]
    assert len(cache) == 2


def test_lru_eviction_bounds_entry_count():
    fetch, calls = make_fetcher()
    cache = SpecCache(fetcher=fetch, max_entries=2)
    cache.get_or_fetch("repo", "1.0")
    cache.get_or_fetch("repo", "2.0")
    cache.get_or_fetch("repo", "1.0")  # touch: 2.0 is now LRU
    cache.get_or_fetch("repo", "3.0")  # evicts 2.0
    assert len(cache) == 2
    cache.get_or_fetch("repo", "2.0")  # was evicted -> must refetch
    assert calls.count(("repo", "2.0")) == 2
    assert cache.misses == 4


def test_oversized_spec_is_refused_and_not_cached():
    big = {"type": "object", "properties": {"blob": {"const": "x" * 10_000}}}
    calls = []

    def fetch(repo, tag):
        calls.append(tag)
        return big

    cache = SpecCache(fetcher=fetch, max_spec_bytes=1_000)
    with pytest.raises(SpecTooLargeError):
        cache.get_or_fetch("repo", "1.0")
    assert len(cache) == 0


def test_fetch_errors_propagate_and_are_not_cached():
    calls = []

    def fetch(repo, tag):
        calls.append(tag)
        raise RuntimeError("registry down")

    cache = SpecCache(fetcher=fetch)
    with pytest.raises(RuntimeError):
        cache.get_or_fetch("repo", "1.0")
    assert len(cache) == 0  # a failure must not poison the cache


def test_invalid_bounds_rejected():
    with pytest.raises(ValueError):
        SpecCache(fetcher=lambda r, t: {}, max_entries=0)
    with pytest.raises(ValueError):
        SpecCache(fetcher=lambda r, t: {}, max_spec_bytes=0)
