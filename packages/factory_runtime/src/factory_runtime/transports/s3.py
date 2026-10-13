"""S3 streaming: a write-once multipart writer with bounded memory, and conditional streaming reads.

Memory while writing is about part_size x (concurrency + 1): one part being filled plus at most `concurrency` parts
uploading. The object only appears when CompleteMultipartUpload succeeds; it is sent with If-None-Match: *, so an
existing object is never replaced (AlreadyExists is raised and the upload is aborted). Any failure aborts the
multipart upload, so no orphaned parts are left behind (the bucket lifecycle also expires incomplete uploads).
"""

from __future__ import annotations

import contextlib
import io
import math
import threading
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from factory_core.errors import DataError

MIN_PART = 5 * 1024 * 1024
DEFAULT_PART = 64 * 1024 * 1024
MAX_PARTS = 10_000
CHUNK = 1024 * 1024


class AlreadyExists(Exception):  # noqa: N818 - a state, not an error the caller always treats as one
    pass


def part_size_for(max_bytes: int, minimum: int = DEFAULT_PART) -> int:
    """Smallest part size >= minimum (whole MiB) such that 10,000 parts cover max_bytes."""
    need = math.ceil(max_bytes / MAX_PARTS)
    size = max(minimum, need, MIN_PART)
    return math.ceil(size / (1024 * 1024)) * 1024 * 1024


def _precondition_failed(exc: BaseException) -> bool:
    r = getattr(exc, "response", None)
    if not isinstance(r, dict):
        return False
    return r.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412 or r.get("Error", {}).get("Code") in (
        "PreconditionFailed",
        "412",
    )


class MultipartWriter(io.RawIOBase):
    """A write-only, unseekable stream into one S3 object (deliberately unseekable: the zip writer streams)."""

    def __init__(
        self,
        s3: Any,
        bucket: str,
        key: str,
        *,
        part_size: int = DEFAULT_PART,
        concurrency: int = 2,
        extra: dict[str, Any] | None = None,
        content_type: str = "application/octet-stream",
    ) -> None:
        super().__init__()
        self.s3 = s3
        self.bucket = bucket
        self.key = key
        self.part_size = max(part_size, MIN_PART)
        self.extra = dict(extra or {})
        self.content_type = content_type
        self._buf = bytearray()
        self._upload_id: str | None = None
        self._parts: dict[int, Future[str]] = {}
        self._pool = ThreadPoolExecutor(max_workers=max(1, concurrency), thread_name_prefix="s3-part")
        self._slots = threading.Semaphore(max(1, concurrency))
        self._done = False
        self.size = 0

    def writable(self) -> bool:
        return True

    def write(self, b: Any) -> int:
        data = bytes(b)
        self._buf += data
        self.size += len(data)
        while len(self._buf) >= self.part_size:
            chunk = bytes(self._buf[: self.part_size])
            del self._buf[: self.part_size]
            self._submit(chunk)
        return len(data)

    def _submit(self, chunk: bytes) -> None:
        if self._upload_id is None:
            res = self.s3.create_multipart_upload(
                Bucket=self.bucket, Key=self.key, ContentType=self.content_type, **self.extra
            )
            self._upload_id = str(res["UploadId"])
        number = len(self._parts) + 1
        self._slots.acquire()

        def upload() -> str:
            try:
                r = self.s3.upload_part(
                    Bucket=self.bucket, Key=self.key, UploadId=self._upload_id, PartNumber=number, Body=chunk
                )
                return str(r["ETag"])
            finally:
                self._slots.release()

        self._parts[number] = self._pool.submit(upload)

    def complete(self) -> None:
        """Make the object visible, write-once. Raises AlreadyExists when the key exists (upload aborted)."""
        try:
            if self._upload_id is None:  # small object: one PUT
                try:
                    self.s3.put_object(
                        Bucket=self.bucket,
                        Key=self.key,
                        Body=bytes(self._buf),
                        ContentType=self.content_type,
                        IfNoneMatch="*",
                        **self.extra,
                    )
                except Exception as exc:
                    if _precondition_failed(exc):
                        raise AlreadyExists(self.key) from None
                    raise
                self._done = True
                return
            if self._buf:
                self._submit(bytes(self._buf))
                self._buf.clear()
            parts = [{"PartNumber": n, "ETag": f.result()} for n, f in sorted(self._parts.items())]
            try:
                self.s3.complete_multipart_upload(
                    Bucket=self.bucket,
                    Key=self.key,
                    UploadId=self._upload_id,
                    MultipartUpload={"Parts": parts},
                    IfNoneMatch="*",
                )
            except Exception as exc:
                if _precondition_failed(exc):
                    raise AlreadyExists(self.key) from None
                raise
            self._done = True
        finally:
            if not self._done:
                self.abort()
            self._pool.shutdown(wait=True)

    def abort(self) -> None:
        if self._upload_id is not None and not self._done:
            for f in self._parts.values():
                f.cancel()
            # best effort: the bucket lifecycle rule expires incomplete uploads anyway
            with contextlib.suppress(Exception):
                self.s3.abort_multipart_upload(Bucket=self.bucket, Key=self.key, UploadId=self._upload_id)
            self._upload_id = None
        self._buf.clear()

    def close(self) -> None:
        if not self._done:
            self.abort()
            self._pool.shutdown(wait=False, cancel_futures=True)
        super().close()


class _Body(io.RawIOBase):
    """A readable stream over an S3 GetObject body that checks the byte count at EOF."""

    def __init__(self, body: Any, expected: int | None, what: str) -> None:
        super().__init__()
        self.body = body
        self.expected = expected
        self.what = what
        self.read_bytes = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        data = self.body.read(len(b))
        n = len(data)
        b[:n] = data
        self.read_bytes += n
        if n == 0 and self.expected is not None and self.read_bytes != self.expected:
            raise DataError(
                "source_size_mismatch", f"{self.what}: read {self.read_bytes} bytes, expected {self.expected}"
            )
        return n

    def close(self) -> None:
        try:
            self.body.close()
        finally:
            super().close()


def open_object(
    s3: Any, bucket: str, key: str, *, if_match: str | None = None, size: int | None = None, what: str = "object"
) -> io.BufferedReader:
    kw: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if if_match:
        kw["IfMatch"] = if_match if if_match.startswith('"') else f'"{if_match}"'
    try:
        res = s3.get_object(**kw)
    except Exception as exc:
        if _precondition_failed(exc):
            raise DataError("source_changed", f"{what} changed after it was listed") from None
        raise
    return io.BufferedReader(_Body(res["Body"], size, what), buffer_size=CHUNK)


def iter_object(s3: Any, bucket: str, key: str, chunk: int = CHUNK) -> Iterator[bytes]:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        while True:
            data = body.read(chunk)
            if not data:
                return
            yield data
    finally:
        body.close()
