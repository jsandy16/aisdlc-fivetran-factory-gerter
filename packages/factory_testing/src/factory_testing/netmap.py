"""Point real DNS names at local stand-ins, in tests only, by patching socket.getaddrinfo.

The runtime has no host-override hook: it connects to `sftp-stub.dev.acme.example:22` exactly as in production, and
TLS checks the certificate against that name (SNI included). Only the address the name resolves to changes here.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_LOCK = threading.Lock()
_MAP: dict[tuple[str, int], tuple[str, int]] = {}
_REAL = socket.getaddrinfo


def _patched(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (str(host).lower().rstrip("."), int(port) if port is not None else 0)
    with _LOCK:
        target = _MAP.get(key)
    if target is not None:
        return _REAL(target[0], target[1], *args, **kwargs)
    return _REAL(host, port, *args, **kwargs)


@contextmanager
def netmap(mapping: dict[tuple[str, int], tuple[str, int]]) -> Iterator[None]:
    """{("sftp-stub.dev.acme.example", 22): ("127.0.0.1", 40123), ...} for the duration of the block."""
    with _LOCK:
        previous = dict(_MAP)
        _MAP.update({(h.lower(), p): t for (h, p), t in mapping.items()})
        socket.getaddrinfo = _patched
    try:
        yield
    finally:
        with _LOCK:
            _MAP.clear()
            _MAP.update(previous)
            if not _MAP:
                socket.getaddrinfo = _REAL
