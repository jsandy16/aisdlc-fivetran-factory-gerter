"""Strict parser for destination URIs returned at run time (e.g. by the self-service portal).

Security-critical (ADR-0024): anything ambiguous is rejected rather than normalised. Accepted forms:
  s3://<bucket>/<key/prefix segments>[/]
  sftp://<host>[:port]/<path segments>[/]
  https://<host>[:port]/<path segments>[/]
Rejected: other schemes, non-ASCII (no IDN homographs), any '%' (no percent-encoding tricks), userinfo, query,
fragment, ';', backslash, whitespace/control characters, empty, '.' or '..' segments, IP-literal hosts, trailing dots.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .errors import SecurityError

DestinationType = Literal["s3", "sftp", "https"]
DEFAULT_PORTS: dict[str, int] = {"sftp": 22, "https": 443}
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_FORBIDDEN = re.compile(r"[%@?#;\\\s\x00-\x1f\x7f]")


@dataclass(frozen=True)
class DestinationUri:
    type: DestinationType
    host: str  # bucket name for s3
    port: int | None  # None for s3
    segments: tuple[str, ...]
    trailing_slash: bool

    @property
    def path(self) -> str:
        return "/".join(self.segments) + ("/" if self.trailing_slash and self.segments else "")


def _fail(msg: str) -> SecurityError:
    return SecurityError(
        "destination_uri_invalid", msg, hint="the portal must return a plain s3://, sftp:// or https:// URI"
    )


def valid_hostname(host: str) -> bool:
    if not host or len(host) > 253 or host.endswith(".") or "." not in host:
        return False
    labels = host.split(".")
    if all(lbl.isdigit() for lbl in labels):  # IPv4 literal
        return False
    return all(_LABEL.match(lbl) for lbl in labels) and not labels[-1].isdigit()


def parse_destination_uri(text: str) -> DestinationUri:
    if not isinstance(text, str) or not text:
        raise _fail("destination is empty or not text")
    if len(text) > 2048:
        raise _fail("destination URI is too long")
    if not text.isascii():
        raise _fail("destination URI contains non-ASCII characters")
    if _FORBIDDEN.search(text):
        raise _fail("destination URI contains a forbidden character (%, @, ?, #, ;, backslash or whitespace)")
    scheme, sep, rest = text.partition("://")
    if not sep or scheme not in ("s3", "sftp", "https"):
        raise _fail("only s3://, sftp:// and https:// destinations are supported")
    authority, slash, path = rest.partition("/")
    if not authority:
        raise _fail("destination URI has no host or bucket")
    if "[" in authority or "]" in authority:
        raise _fail("IP-literal hosts are not allowed")
    port: int | None = None
    if scheme == "s3":
        if ":" in authority or not _BUCKET.match(authority) or ".." in authority:
            raise _fail("not a valid S3 bucket name")
        host = authority
    else:
        host_part, colon, port_text = authority.partition(":")
        host = host_part.lower()
        if colon:
            if not port_text.isdigit() or not (1 <= int(port_text) <= 65535) or port_text.startswith("0"):
                raise _fail("invalid port")
            port = int(port_text)
        else:
            port = DEFAULT_PORTS[scheme]
        if not valid_hostname(host):
            raise _fail("not a valid DNS host name (IP literals and trailing dots are not allowed)")
    trailing = path.endswith("/")
    raw = path[:-1] if trailing else path
    segments: tuple[str, ...] = tuple(raw.split("/")) if raw else ()
    for seg in segments:
        if seg in ("", ".", ".."):
            raise _fail("empty, '.' or '..' path segments are not allowed")
        if not _SEGMENT.match(seg):
            raise _fail("path segments may only contain letters, digits, '.', '_' and '-'")
    if not slash and segments:  # pragma: no cover - partition guarantees slash when path is non-empty
        raise _fail("malformed destination URI")
    return DestinationUri(scheme, host, port, segments, trailing)  # type: ignore[arg-type]
