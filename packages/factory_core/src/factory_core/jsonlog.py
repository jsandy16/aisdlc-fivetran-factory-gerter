"""JSON-lines logging with secret redaction and CloudWatch embedded metrics.

Every event is one JSON object per line: `ts`, `level`, `event`, the fields bound for the current run/task/rule
(contextvars, so threads and Lambda warm starts do not leak fields), and the event's own fields.

Redaction is applied to the serialized line, last, so nothing can bypass it:
- every registered secret value is masked, together with its JSON-escaped form and every line longer than 8
  characters of a multi-line value (adapted from pipeline-factory 0.1.0 `pf/deploy.py _mask`);
- known secret shapes are masked even when never registered (adapted from `pf/llm/base.py SECRET_PATTERNS`).

Secret values are registered when they are fetched (see factory_runtime.secrets), not when they are used. Callers still
log ids, counts and hashes, never data values or secrets: redaction is the safety net, not the design.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import IO, Any

MASK = "***"
MIN_SECRET_LEN = 4
_LINE_MIN = 8

_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), MASK),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[^\n\"]*"), MASK),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), MASK),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), MASK),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + MASK),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|api[_ -]?key|token|access[_ -]?key)\b(\s*(?:is|:|=)\s*)([^\s,;\"]{4,})"
        ),
        r"\1\2" + MASK,
    ),
]

THIRD_PARTY_LOGGERS = ("botocore", "boto3", "s3transfer", "urllib3", "httpx", "httpcore", "paramiko", "asyncssh")


class Redactor:
    """Thread-safe registry of secret strings to mask in every log line."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()
        self._ordered: list[str] = []

    def register(self, value: str) -> None:
        if not isinstance(value, str) or len(value) < MIN_SECRET_LEN:
            return
        forms = {value, json.dumps(value)[1:-1]}
        if "\n" in value:
            forms |= {ln.strip() for ln in value.splitlines() if len(ln.strip()) > _LINE_MIN and "-----" not in ln}
        with self._lock:
            new = forms - self._values
            if new:
                self._values |= new
                self._ordered = sorted(self._values, key=len, reverse=True)  # longest first

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._ordered = []

    def redact(self, text: str) -> str:
        with self._lock:
            ordered = list(self._ordered)
        for v in ordered:
            if v in text:
                text = text.replace(v, MASK)
        for pat, repl in _PATTERNS:
            text = pat.sub(repl, text)
        return text

    def __len__(self) -> int:
        return len(self._values)


REDACTOR = Redactor()
_FIELDS: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("factory_log_fields", default=None)


def register_secret(value: str) -> None:
    REDACTOR.register(value)


def redact(text: str) -> str:
    return REDACTOR.redact(text)


@contextlib.contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Add fields to every event logged inside the block (per thread / task / Lambda invocation)."""
    token = _FIELDS.set({**(_FIELDS.get() or {}), **fields})
    try:
        yield
    finally:
        _FIELDS.reset(token)


def reset_bound() -> None:
    _FIELDS.set(None)


def _scalar(v: Any) -> Any:
    if v is None or isinstance(v, bool | int | float | str):
        return v
    if isinstance(v, list | tuple):
        return [_scalar(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _scalar(x) for k, x in v.items()}
    return str(v)


class JsonLogger:
    def __init__(
        self,
        stream: IO[str] | None = None,
        *,
        clock: Callable[[], float] = time.time,
        static: dict[str, Any] | None = None,
    ) -> None:
        self.stream = stream
        self.clock = clock
        self.static = dict(static or {})
        self._lock = threading.Lock()

    def _ts(self) -> str:
        t = self.clock()
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"

    def write(self, doc: dict[str, Any]) -> str:
        line = redact(json.dumps(doc, sort_keys=False, separators=(",", ":"), ensure_ascii=False))
        stream = self.stream or sys.stdout
        with self._lock:
            stream.write(line + "\n")
            stream.flush()
        return line

    def event(self, name: str, level: str = "INFO", **fields: Any) -> str:
        doc: dict[str, Any] = {"ts": self._ts(), "level": level, "event": name}
        doc.update(_scalar(self.static))
        doc.update(_scalar(_FIELDS.get() or {}))
        doc.update({k: _scalar(v) for k, v in fields.items()})
        return self.write(doc)

    def metric(self, name: str, value: float | int, unit: str = "Count", **dimensions: str) -> str:
        """CloudWatch embedded metric format: one line, picked up from the log group without API calls."""
        dims = {k: str(v) for k, v in sorted(dimensions.items())}
        doc: dict[str, Any] = {
            "_aws": {
                "Timestamp": int(self.clock() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": "PipelineFactory",
                        "Dimensions": [list(dims)],
                        "Metrics": [{"Name": name, "Unit": unit}],
                    }
                ],
            },
            **dims,
            name: value,
        }
        return self.write(doc)


class _Bridge(logging.Handler):
    """Routes stdlib logging (third-party libraries) through the JSON logger, so redaction applies to it too."""

    def __init__(self, logger: JsonLogger) -> None:
        super().__init__(level=logging.WARNING)
        self.logger = logger

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # a broken format string must not break the run
            msg = str(record.msg)
        self.logger.event("library_log", level=record.levelname, library=record.name, message=msg[:2000])


_LOGGER = JsonLogger()


def get_logger() -> JsonLogger:
    return _LOGGER


def configure(stream: IO[str] | None = None, **static: Any) -> JsonLogger:
    """Install the process logger: JSON lines to `stream`, third-party loggers clamped to WARNING and bridged."""
    global _LOGGER
    _LOGGER = JsonLogger(stream, static=static)
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, _Bridge):
            root.removeHandler(h)
    root.addHandler(_Bridge(_LOGGER))
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return _LOGGER
