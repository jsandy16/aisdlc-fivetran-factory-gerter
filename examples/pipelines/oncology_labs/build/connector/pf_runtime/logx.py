"""Structured, value-free logging on top of the Fivetran SDK logger.

One line per event: `pf event=<name> k=v ...`. Callers pass identifiers and counts only — never data.
INFO is rate-limited below Fivetran's 1500/minute cap; excess lines are counted and summarised.
"""
from __future__ import annotations

import threading
import time
from typing import Any


class Log:
    def __init__(self, sdk_log: Any, max_info_per_minute: int = 1000):
        self._log = sdk_log
        self._max = max_info_per_minute
        self._window = time.monotonic()
        self._count = 0
        self._dropped = 0
        self._lock = threading.Lock()

    @staticmethod
    def _fmt(event: str, fields: dict[str, Any]) -> str:
        parts = [f"pf event={event}"]
        for k, v in fields.items():
            s = str(v)
            if " " in s or "=" in s:
                s = '"' + s.replace('"', "'") + '"'
            parts.append(f"{k}={s}")
        return " ".join(parts)

    def info(self, event: str, **fields: Any) -> None:
        with self._lock:
            now = time.monotonic()
            if now - self._window >= 60:
                if self._dropped:
                    self._log.warning(self._fmt("log_rate_limited", {"dropped": self._dropped}))
                self._window, self._count, self._dropped = now, 0, 0
            if self._count >= self._max:
                self._dropped += 1
                return
            self._count += 1
        self._log.info(self._fmt(event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self._log.warning(self._fmt(event, fields))

    def error(self, event: str, **fields: Any) -> None:
        self._log.severe(self._fmt(event, fields)) if not hasattr(self._log, "error") else self._log.error(self._fmt(event, fields))
