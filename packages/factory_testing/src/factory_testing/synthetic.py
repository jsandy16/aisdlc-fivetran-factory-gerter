"""Deterministic synthetic study packages (no real data, ever): nested folders, mixed file types, seeded content."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class SyntheticFile:
    rel_path: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def _bytes(seed: str, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        counter += 1
    return bytes(out[:n])


def study_package(study_id: str = "ONC101", package_id: str = "PKG0042", *, files: int = 12) -> list[SyntheticFile]:
    """A small, varied package: CSV-like text, a PNG-like image (stored, not deflated), nested folders."""
    out = []
    for i in range(files):
        folder = ["", "visits/", "visits/2026/", "imaging/", "docs/"][i % 5]
        if folder == "imaging/":
            rel, data = f"{folder}scan_{i:03d}.png", PNG_HEADER + _bytes(f"{package_id}-{i}", 3000 + i)
        else:
            rows = "".join(f"{study_id},SUBJ{n:04d},{n * 7 % 97}\n" for n in range(40 + i))
            rel, data = f"{folder}data_{i:03d}.csv", f"study,subject,value\n{rows}".encode()
        out.append(SyntheticFile(rel, data))
    return sorted(out, key=lambda f: f.rel_path)


def put_package(s3: Any, bucket: str, prefix: str, files: list[SyntheticFile]) -> None:
    for f in files:
        s3.put_object(Bucket=bucket, Key=prefix + f.rel_path, Body=f.data)


class GeneratedStream(io.RawIOBase):
    """`size` bytes of cheap, incompressible-ish content generated on the fly (for large-volume tests)."""

    def __init__(self, size: int, seed: int = 1) -> None:
        super().__init__()
        self.remaining = size
        self.block = _bytes(f"block-{seed}", 1 << 20)  # random-looking; its 1 MiB period defeats deflate's window
        self.pos = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        n = min(len(b), self.remaining, len(self.block) - self.pos)
        b[:n] = self.block[self.pos : self.pos + n]
        self.pos = (self.pos + n) % len(self.block)
        self.remaining -= n
        return n


def chunks(size: int, chunk: int = 1 << 20) -> Iterator[bytes]:
    s = GeneratedStream(size)
    while True:
        data = s.read(chunk)
        if not data:
            return
        yield data
