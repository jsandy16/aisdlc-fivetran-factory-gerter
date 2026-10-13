"""HTTPS delivery (ADR-0024): write-once PUT, verified with the best evidence the server offers (decision P3-3).

1. HEAD the target first: when the file exists, compare instead of sending 10 GB that a 412 would reject at the end.
2. Stream the package with Content-Length, `If-None-Match: *` (write_once: if_none_match) and `Repr-Digest`
   (RFC 9530); a 412 means it exists: compare.
3. Verify: a `Repr-Digest` returned by the server is compared (digest); otherwise a GET read-back is hashed when the
   endpoint allows it (read_back); otherwise only the size from HEAD is checked (size_only); an endpoint that answers
   neither HEAD nor GET gives no evidence at all (none). The receipt and the audit event record which.
`write_once: none` means check (HEAD), then PUT: there is a small window in which two writers could both write.
The bearer token is revealed per request only; it never sits in a header dict between requests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx
from factory_core.errors import ConfigError, DataError, ErrorClass, FactoryError, SecurityError, TransientError
from factory_core.retry import classify, classify_http_status

from ..secrets.value import SecretValue
from .common import CHUNK, Outcome, PutResult, Source, Verification, verified_chunks


def _b64_sha256(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode()


def digest_of(headers: httpx.Headers) -> str | None:
    """sha-256 from a Repr-Digest header (`sha-256=:<base64>:`), as hex."""
    for part in headers.get("Repr-Digest", "").split(","):
        k, _, v = part.strip().partition("=")
        if k.strip().lower() == "sha-256" and v.startswith(":") and v.endswith(":"):
            try:
                raw = base64.b64decode(v[1:-1], validate=True)
            except ValueError:
                return None
            return raw.hex() if len(raw) == 32 else None
    return None


@dataclass(frozen=True)
class Remote:
    """What HEAD says about the target: exists (None = the server does not answer HEAD), size, sha256."""

    exists: bool | None
    size: int | None = None
    sha256: str | None = None


class HttpsTarget:
    def __init__(
        self,
        entry: Any,
        folder: tuple[str, ...],
        client: httpx.Client,
        bearer: SecretValue | None = None,
    ) -> None:
        self.entry = entry
        self.base = f"https://{entry.host}" + ("" if entry.port == 443 else f":{entry.port}")
        self.folder = "/" + "/".join(quote(s, safe="") for s in folder)
        self.client = client
        self.bearer = bearer

    def close(self) -> None:
        self.client.close()

    def uri(self, name: str) -> str:
        return f"{self.base}{self.folder}/{quote(name, safe='')}"

    # ------------------------------------------------------------------ HTTP

    def _transport_error(self, exc: Exception) -> FactoryError:
        cls = classify(exc)
        if cls is ErrorClass.SECURITY:
            return SecurityError("tls_untrusted", f"{self.entry.host}: the server certificate is not trusted")
        if cls is ErrorClass.CONFIG:
            return ConfigError("tls_client_rejected", f"{self.entry.host} rejected the client certificate")
        return TransientError("https_unavailable", f"cannot reach {self.entry.host} ({type(exc).__name__})")

    def _checked(self, res: httpx.Response) -> httpx.Response:
        if 300 <= res.status_code < 400:
            raise ConfigError("redirect_refused", f"{self.entry.host} answered with a redirect; not followed")
        if res.status_code in (401, 403):
            raise ConfigError("delivery_auth_refused", f"{self.entry.host} refused the delivery credentials")
        return res

    def _request(self, method: str, url: str, headers: dict[str, str] | None = None, **kw: Any) -> httpx.Response:
        h = dict(headers or {})
        try:
            if self.bearer is None:
                return self._checked(self.client.request(method, url, headers=h, **kw))
            with self.bearer.reveal() as token:
                h["Authorization"] = f"Bearer {token}"
                return self._checked(self.client.request(method, url, headers=h, **kw))
        except httpx.TransportError as exc:
            raise self._transport_error(exc) from exc

    def _status_error(self, status: int, what: str) -> FactoryError:
        if classify_http_status(status) is ErrorClass.TRANSIENT:
            return TransientError("https_unavailable", f"{self.entry.host} {what} failed ({status})")
        return ConfigError("delivery_request_failed", f"{self.entry.host} {what} failed ({status})")

    def _head(self, url: str) -> Remote:
        res = self._request("HEAD", url)
        if res.status_code == 404:
            return Remote(False)
        if res.status_code in (405, 501):  # the endpoint does not answer HEAD
            return Remote(None)
        if res.status_code >= 400:
            raise self._status_error(res.status_code, "HEAD")
        length = res.headers.get("Content-Length", "")
        return Remote(True, int(length) if length.isdigit() else None, digest_of(res.headers))

    def _read_back(self, url: str) -> tuple[int, str] | None:
        """Hash the stored file by GET; None when the endpoint does not allow GET."""
        h: dict[str, str] = {}
        try:
            if self.bearer is None:
                return self._hash_get(url, h)
            with self.bearer.reveal() as token:
                h["Authorization"] = f"Bearer {token}"
                return self._hash_get(url, h)
        except httpx.TransportError as exc:
            raise self._transport_error(exc) from exc

    def _hash_get(self, url: str, headers: dict[str, str]) -> tuple[int, str] | None:
        with self.client.stream("GET", url, headers=headers) as res:
            if res.status_code != 200:
                return None
            h = hashlib.sha256()
            n = 0
            for chunk in res.iter_bytes(CHUNK):
                h.update(chunk)
                n += len(chunk)
            return n, h.hexdigest()

    def _compare(self, url: str, size: int, sha256: str) -> tuple[bool | None, Verification | Literal["absent"]]:
        """(None, "absent") or (None, "none") when absent or unseen; otherwise (equal, the evidence used)."""
        remote = self._head(url)
        if remote.exists is False:
            return None, "absent"
        if remote.sha256 is not None:
            same = hmac.compare_digest(remote.sha256, sha256) and remote.size in (None, size)
            return same, "digest"
        read = self._read_back(url)
        if read is not None:
            return read[0] == size and hmac.compare_digest(read[1], sha256), "read_back"
        if remote.exists is None:
            return None, "none"
        if remote.size is not None:
            return remote.size == size, "size_only"
        return False, "none"  # it exists, but nothing can show it is ours

    # ------------------------------------------------------------------ writes

    def _conflict(self, name: str, how: str) -> DataError:
        if how == "none":
            return DataError(
                "destination_exists_unverifiable", f"{name} exists at the destination and cannot be compared"
            )
        return DataError("destination_conflict", f"{name} already exists at the destination with other content")

    def _upload(self, name: str, size: int, sha256: str, body: Iterator[bytes] | bytes, content_type: str) -> Outcome:
        url = self.uri(name)
        same, how = self._compare(url, size, sha256)
        if same is not None:
            if same:
                return "already_present"
            raise self._conflict(name, how)
        headers = {
            "Content-Length": str(size),
            "Content-Type": content_type,
            "Repr-Digest": f"sha-256=:{_b64_sha256(sha256)}:",
        }
        if self.entry.write_once == "if_none_match":
            headers["If-None-Match"] = "*"
        res = self._request(self.entry.method, url, headers=headers, content=body)
        if res.status_code == 412:
            same, how = self._compare(url, size, sha256)
            if same:
                return "already_present"
            raise self._conflict(name, how)
        if res.status_code >= 400:
            raise self._status_error(res.status_code, self.entry.method)
        return "written"

    def put_package(self, name: str, src: Source, *, verify: bool) -> PutResult:
        outcome = self._upload(name, src.size, src.sha256, verified_chunks(src), "application/zip")
        url = self.uri(name)
        if verify:
            same, how = self._compare(url, src.size, src.sha256)
        else:
            remote = self._head(url)
            if remote.exists is False:
                same, how = None, "absent"
            elif remote.size is not None:
                same, how = remote.size == src.size, "size_only"
            else:
                same, how = None, "none"
        if how == "absent":
            raise DataError("delivery_verification_failed", f"{name} is not at the destination after writing")
        if same is False:
            raise DataError("delivery_verification_failed", f"{name} at the destination differs from the package")
        return PutResult(outcome, how, url)

    def put_small(self, name: str, data: bytes, content_type: str) -> Outcome:
        return self._upload(name, len(data), hashlib.sha256(data).hexdigest(), data, content_type)
