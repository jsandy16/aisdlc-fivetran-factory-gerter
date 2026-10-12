"""Exponential backoff with full jitter, for transient errors only."""
from __future__ import annotations

import random
import socket
import time
from typing import Callable, TypeVar

from .errors import ConfigError, TransientError

T = TypeVar("T")

_TRANSIENT_NAMES = {
    # paramiko / ssh
    "SSHException", "ChannelException", "ProxyCommandFailure", "NoValidConnectionsError",
    # ftplib temporary replies
    "error_temp",
    # database drivers
    "OperationalError", "InterfaceError",
    # botocore
    "EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
}
_FATAL_NAMES = {"AuthenticationException", "BadHostKeyException", "error_perm"}


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, ConfigError):
        return False
    if isinstance(exc, TransientError):
        return True
    names = {c.__name__ for c in type(exc).__mro__}
    if names & _FATAL_NAMES:
        return False
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError, EOFError)):
        return True
    if names & _TRANSIENT_NAMES:
        return True
    code = getattr(exc, "response", {}) or {}
    if isinstance(code, dict):
        status = code.get("ResponseMetadata", {}).get("HTTPStatusCode")
        err = code.get("Error", {}).get("Code", "")
        if status in (429, 500, 502, 503, 504) or err in ("SlowDown", "Throttling", "RequestTimeout"):
            return True
    return False


def retry(fn: Callable[[], T], *, attempts: int, base: float, maximum: float, what: str, log=None,
          sleep: Callable[[float], None] = time.sleep) -> T:
    """Run fn; retry transient failures up to `attempts` times. Non-transient errors propagate at once."""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 - classified below
            if not is_transient(exc) or attempt == attempts:
                raise
            last = exc
            delay = random.uniform(0, min(maximum, base * (2 ** (attempt - 1))))
            if log:
                log.warning("retry", what=what, attempt=attempt, of=attempts, error=type(exc).__name__, sleep_s=round(delay, 2))
            sleep(delay)
    raise AssertionError(last)  # pragma: no cover
