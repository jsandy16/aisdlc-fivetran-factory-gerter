"""What every delivery target offers and returns, and the hash-checked stream they all send."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import IO, Literal, Protocol

from factory_core.errors import DataError

CHUNK = 1024 * 1024
Outcome = Literal["written", "already_present"]
Verification = Literal["read_back", "digest", "size_only", "server_checksum", "none"]


@dataclass(frozen=True)
class PutResult:
    outcome: Outcome
    verification: Verification
    target: str


@dataclass(frozen=True)
class Source:
    """The staged package being delivered: where it is, what it must hash to, and how to stream it."""

    bucket: str
    key: str
    size: int
    sha256: str
    open: Callable[[], IO[bytes]]


class Target(Protocol):
    def put_package(self, name: str, src: Source, *, verify: bool) -> PutResult: ...

    def put_small(self, name: str, data: bytes, content_type: str) -> Outcome: ...

    def uri(self, name: str) -> str: ...

    def close(self) -> None: ...


def verified_chunks(src: Source, chunk: int = CHUNK) -> Iterator[bytes]:
    """The package's bytes; the last chunk is held back until the whole stream is hashed.

    A staged package that no longer matches its hash record raises before its last byte is sent, so the receiving end
    never sees a complete wrong file (the SFTP temp file is removed; an HTTPS body stays short of its Content-Length).
    """
    h = hashlib.sha256()
    size = 0
    with src.open() as fh:
        held = fh.read(chunk)
        while held:
            nxt = fh.read(chunk)
            h.update(held)
            size += len(held)
            if not nxt and (size != src.size or not hmac.compare_digest(h.hexdigest(), src.sha256)):
                raise DataError("staged_package_changed", "the staged package no longer matches its hash record")
            yield held
            held = nxt
    if size == 0 and (src.size != 0 or not hmac.compare_digest(h.hexdigest(), src.sha256)):
        raise DataError("staged_package_changed", "the staged package no longer matches its hash record")
