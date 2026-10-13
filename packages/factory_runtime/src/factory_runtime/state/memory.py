"""In-process run-state with the same semantics as the DynamoDB store (unit tests, local runs)."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Any

from .protocol import Record


class MemoryRunState:
    def __init__(self, pipeline_id: str) -> None:
        self.pipeline_id = pipeline_id
        self._items: dict[tuple[str, str], Record] = {}
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()

    def _pk(self, run_dir: str) -> str:
        return f"{self.pipeline_id}#{run_dir}"

    def get(self, run_dir: str, sk: str) -> Record | None:
        with self._lock:
            return self._items.get((self._pk(run_dir), sk))

    def put(self, run_dir: str, sk: str, doc: dict[str, Any], *, state: str | None = None) -> None:
        with self._lock:
            self._items[(self._pk(run_dir), sk)] = Record(sk, dict(doc), state)

    def put_if_absent(
        self, run_dir: str, sk: str, doc: dict[str, Any], *, state: str | None = None
    ) -> tuple[bool, Record]:
        key = (self._pk(run_dir), sk)
        with self._lock:
            existing = self._items.get(key)
            if existing is not None:
                return False, existing
            rec = Record(sk, dict(doc), state)
            self._items[key] = rec
            return True, rec

    def transition(
        self,
        run_dir: str,
        sk: str,
        *,
        allowed_from: Iterable[str | None],
        to_state: str,
        doc: dict[str, Any] | None = None,
    ) -> bool:
        key = (self._pk(run_dir), sk)
        allowed = set(allowed_from)
        with self._lock:
            cur = self._items.get(key)
            cur_state = cur.state if cur else None
            if cur_state not in allowed:
                return False
            base = dict(cur.doc) if cur else {}
            self._items[key] = Record(sk, {**base, **(doc or {})}, to_state)
            return True

    def allocate_seq(self, name: str, run_dir: str) -> int:
        key = (f"{self.pipeline_id}#SEQ#{name}", f"RUN#{run_dir}")
        with self._lock:
            existing = self._items.get(key)
            if existing is not None:
                return int(existing.doc["n"])
            n = self._counters.get(name, 0) + 1
            self._counters[name] = n
            self._items[key] = Record(key[1], {"n": n})
            return n

    def items(self) -> list[tuple[str, str, Record]]:
        with self._lock:
            return [(pk, sk, r) for (pk, sk), r in sorted(self._items.items())]
