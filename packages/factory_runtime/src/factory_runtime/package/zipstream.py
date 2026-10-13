"""Deterministic streaming zip (ADR-0013): the same files always give the same zip bytes, at any size.

- written to an unseekable sink, so every entry has a data descriptor and nothing is ever re-read or held in memory;
- fixed metadata: date 1980-01-01 00:00:00, create_system 3, mode 0644 regular file, entries in path order;
- `zip64: auto` uses zip64 only when an entry needs it (size taken from the listing), `always` forces it;
- already-compressed files (extension, magic bytes, or a first 256 KiB that does not compress) are stored
  when store_if_compressed is set;
- the zip's MD5 and SHA-256 are computed while it is written (a tee), each entry's SHA-256 and MD5 while it is read.

Byte-identical output holds for the same runtime image: deflate output depends on the zlib build, so the zlib
version is recorded with the package. Stored members carry data descriptors, which some streaming readers (Java
ZipInputStream) reject; `store_if_compressed: false` avoids stored members if a consumer needs that.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import IO, Any

from factory_core.errors import DataError
from factory_spec.artifact_models import PackageMember

FIXED_TIME = (1980, 1, 1, 0, 0, 0)
CHUNK = 1024 * 1024
COMPRESSED_EXT = frozenset(
    {
        ".zip", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z", ".rar", ".png", ".jpg", ".jpeg", ".gif", ".webp",
        ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".docx", ".xlsx", ".pptx", ".jar", ".parquet", ".avro", ".orc",
    }
)  # fmt: skip
_MAGIC = (
    b"PK\x03\x04",
    b"\x1f\x8b",
    b"BZh",
    b"\xfd7zXZ\x00",
    b"\x28\xb5\x2f\xfd",
    b"7z\xbc\xaf\x27\x1c",
    b"\x89PNG",
    b"\xff\xd8\xff",
    b"PAR1",
)


SAMPLE = 256 * 1024
INCOMPRESSIBLE = 0.95


def is_compressed(path: str, head: bytes) -> bool:
    """Known compressed formats (extension or magic bytes), or content whose first 256 KiB does not shrink by 5%.

    The sample test depends only on the content, so the decision (and the zip) stays deterministic.
    """
    lower = path.lower()
    if any(lower.endswith(ext) for ext in COMPRESSED_EXT):
        return True
    if head.startswith(_MAGIC) or head[4:8] == b"ftyp":
        return True
    sample = head[:SAMPLE]
    return len(sample) >= 64 * 1024 and len(zlib.compress(sample, 1)) > INCOMPRESSIBLE * len(sample)


class HashingSink(io.RawIOBase):
    """Unseekable tee: forwards writes and keeps MD5, SHA-256 and the byte count of everything written."""

    def __init__(self, target: IO[bytes] | Any) -> None:
        super().__init__()
        self.target = target
        self.md5 = hashlib.md5(usedforsecurity=False)
        self.sha256 = hashlib.sha256()
        self.size = 0

    def writable(self) -> bool:
        return True

    def write(self, b: Any) -> int:
        data = bytes(b)
        self.md5.update(data)
        self.sha256.update(data)
        self.size += len(data)
        self.target.write(data)
        return len(data)


@dataclass
class ZipOptions:
    compression: str = "deflate"  # deflate | store
    level: int = 6
    store_if_compressed: bool = True
    zip64: str = "auto"  # auto | always


class DeterministicZip:
    def __init__(self, sink: IO[bytes] | Any, options: ZipOptions) -> None:
        self.options = options
        self.sink = sink
        self.zf = zipfile.ZipFile(sink, "w", allowZip64=True)
        self.members: list[PackageMember] = []
        self._last: str | None = None
        self.used_zip64 = options.zip64 == "always"

    def add(self, path: str, size: int, stream: IO[bytes]) -> PackageMember:
        if self._last is not None and path <= self._last:
            raise DataError("package_order", "package entries must be added in path order without duplicates")
        self._last = path
        head = stream.read(CHUNK)
        store = self.options.compression == "store" or (self.options.store_if_compressed and is_compressed(path, head))
        zi = zipfile.ZipInfo(path, date_time=FIXED_TIME)
        zi.create_system = 3
        zi.external_attr = 0o100644 << 16
        zi.compress_type = zipfile.ZIP_STORED if store else zipfile.ZIP_DEFLATED
        if not store:
            zi._compresslevel = self.options.level  # type: ignore[attr-defined]  # stable across 3.11-3.13
        zi.file_size = size  # lets zip64 "auto" decide from the declared size
        force = self.options.zip64 == "always"
        if size * 1.05 > zipfile.ZIP64_LIMIT:
            self.used_zip64 = True
        sha256 = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        n = 0
        with self.zf.open(zi, "w", force_zip64=force) as dst:
            chunk = head
            while chunk:
                sha256.update(chunk)
                md5.update(chunk)
                n += len(chunk)
                dst.write(chunk)
                chunk = stream.read(CHUNK)
        if n != size:
            raise DataError("source_size_mismatch", f"'{path}' had {n} bytes; the listing said {size}")
        member = PackageMember(
            path=path,
            size=n,
            sha256=sha256.hexdigest(),
            md5=md5.hexdigest(),
            compression="store" if store else "deflate",
        )
        self.members.append(member)
        return member

    def close(self) -> None:
        self.zf.close()

    def abandon(self) -> None:
        """After a failure: drop the archive without writing its directory into a sink that is being discarded."""
        self.zf.fp = None  # zipfile.close() and __del__ do nothing once fp is None


def zip_files(files: Iterable[tuple[str, int, IO[bytes]]], sink: Any, options: ZipOptions) -> list[PackageMember]:
    """Convenience: zip (path, size, stream) items, already in path order, into sink."""
    z = DeterministicZip(sink, options)
    for path, size, stream in files:
        try:
            z.add(path, size, stream)
        finally:
            stream.close()
    z.close()
    return z.members


ZLIB_VERSION = zlib.ZLIB_RUNTIME_VERSION
