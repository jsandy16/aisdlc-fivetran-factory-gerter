"""In-process secret cache (never on disk). TTL per kind; `refresh=True` refetches once after an auth failure."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .provider import ResolvedRef, SecretProvider
from .value import SecretValue

DEFAULT_TTL = 300
TTL_BY_KIND = {"oauth_client": 300, "api_key": 300, "password": 300, "ssh_private_key": 900, "tls_client_key": 900}


class CachedProvider:
    def __init__(self, inner: SecretProvider, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.inner = inner
        self.clock = clock
        self._entries: dict[tuple[str, str, str | None, str | None], tuple[float, SecretValue]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(ref: ResolvedRef) -> tuple[str, str, str | None, str | None]:
        return (ref.provider, ref.path, ref.key, ref.version)

    def get(self, ref: ResolvedRef, *, purpose: str, rule_id: str | None = None, refresh: bool = False) -> SecretValue:
        key = self._key(ref)
        now = self.clock()
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None and not refresh and now - hit[0] < TTL_BY_KIND.get(ref.kind, DEFAULT_TTL):
                return hit[1]
        value = self.inner.get(ref, purpose=purpose, rule_id=rule_id, refresh=refresh)
        with self._lock:
            self._entries[key] = (now, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
