"""Bounded, TTL-based cache for connector specs.

Ensures a spec is never pulled on every sync: within the TTL a cached
entry is served, and the cache itself is bounded both in number of entries
(LRU eviction) and in the size of any single stored spec.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import Callable, Optional

DEFAULT_TTL_SECONDS = 3600.0
DEFAULT_MAX_ENTRIES = 256
# A connectionSpecification is normally tens of KB; 1 MiB is a generous cap
# that still prevents an unbounded/broken spec from exhausting memory.
DEFAULT_MAX_SPEC_BYTES = 1_048_576


class SpecTooLargeError(ValueError):
    pass


def spec_size_bytes(spec: dict) -> int:
    return len(json.dumps(spec, default=str).encode("utf-8"))


class SpecCache:
    """LRU + TTL cache keyed by ``(docker_repository, docker_image_tag)``.

    ``fetcher`` is called on a miss/expired entry and must return the spec
    dict; exceptions propagate to the caller (the detector turns them into
    BLOCKED reports). Oversized specs are refused and not cached.
    """

    def __init__(
        self,
        fetcher: Callable[[str, str], dict],
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_spec_bytes: int = DEFAULT_MAX_SPEC_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        if max_spec_bytes < 1:
            raise ValueError("max_spec_bytes must be >= 1")
        self._fetcher = fetcher
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._max_spec_bytes = max_spec_bytes
        self._clock = clock
        # key -> (expiry, spec)
        self._entries: OrderedDict[tuple[str, str], tuple[float, dict]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)

    def get_or_fetch(self, docker_repository: str, docker_image_tag: str) -> dict:
        key = (docker_repository, docker_image_tag)
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None:
            expiry, spec = entry
            if now < expiry:
                self._entries.move_to_end(key)
                self.hits += 1
                return spec
            del self._entries[key]

        self.misses += 1
        spec = self._fetcher(docker_repository, docker_image_tag)
        size = spec_size_bytes(spec)
        if size > self._max_spec_bytes:
            raise SpecTooLargeError(
                f"spec for {docker_repository}:{docker_image_tag} is {size} bytes, "
                f"over the {self._max_spec_bytes}-byte cap; refusing to cache"
            )
        self._entries[key] = (now + self._ttl, spec)
        self._evict()
        return spec

    def invalidate(self, docker_repository: str, docker_image_tag: str) -> None:
        self._entries.pop((docker_repository, docker_image_tag), None)

    def _evict(self) -> None:
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
