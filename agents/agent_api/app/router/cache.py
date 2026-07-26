"""Bounded process-local cache for successful router classifications."""

from __future__ import annotations

import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable
from threading import Lock
from typing import Optional

from agents.agent_api.app.router.prompt import RouterDecision

CACHE_MAX_ENTRIES = 1024
CACHE_TTL_SECONDS = 300.0

RouterCacheKey = tuple[str, tuple[str, ...], str, str]


def normalize_router_query(query: str) -> str:
    """Canonicalize harmless query differences without changing semantics."""

    normalized = unicodedata.normalize("NFKC", query).casefold()
    return " ".join(normalized.split())


def _cache_key(
    query: str,
    active_providers: Iterable[str],
    routing_preferences: str,
    prompt_schema_fingerprint: str,
) -> RouterCacheKey:
    return (
        normalize_router_query(query),
        tuple(sorted(active_providers)),
        routing_preferences,
        prompt_schema_fingerprint,
    )


class RouterCache:
    """Thread-safe LRU/TTL cache with copy-on-read decision isolation."""

    def __init__(
        self,
        max_entries: int = CACHE_MAX_ENTRIES,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be greater than zero")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._store: OrderedDict[RouterCacheKey, tuple[RouterDecision, float]] = (
            OrderedDict()
        )
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def get(
        self,
        query: str,
        *,
        active_providers: Iterable[str],
        routing_preferences: str,
        prompt_schema_fingerprint: str,
    ) -> Optional[RouterDecision]:
        key = _cache_key(
            query,
            active_providers,
            routing_preferences,
            prompt_schema_fingerprint,
        )
        now = self._clock()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            decision, created_at = entry
            if now - created_at >= self._ttl_seconds:
                self._store.pop(key, None)
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return decision.model_copy(deep=True)

    def put(
        self,
        query: str,
        decision: RouterDecision,
        *,
        active_providers: Iterable[str],
        routing_preferences: str,
        prompt_schema_fingerprint: str,
    ) -> bool:
        """Store only certain, schema-valid classifier results."""

        if decision.uncertain:
            return False
        key = _cache_key(
            query,
            active_providers,
            routing_preferences,
            prompt_schema_fingerprint,
        )
        with self._lock:
            self._store[key] = (decision.model_copy(deep=True), self._clock())
            self._store.move_to_end(key)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)
        return True

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._store),
            }


_process_cache = RouterCache()


def get_router_cache() -> RouterCache:
    return _process_cache


def reset_router_cache() -> None:
    _process_cache.clear()


__all__ = [
    "CACHE_MAX_ENTRIES",
    "CACHE_TTL_SECONDS",
    "RouterCache",
    "get_router_cache",
    "normalize_router_query",
    "reset_router_cache",
]
