"""Canonical JSON and content hashes (adapted from pipeline-factory 0.1.0 `pf/lock.py`).

Canonical form: sorted keys, compact separators, UTF-8, no NaN. Unlike pf's `default=str`, anything that is not plain
JSON (floats, dates, Decimals, custom objects) is rejected, so a type leak can never change a hash silently.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

JsonValue = None | bool | int | str | list["JsonValue"] | dict[str, "JsonValue"]


class CanonicalError(TypeError):
    """A value that has no canonical JSON form (float, date, Decimal, object, non-string key)."""


def _check(node: Any, loc: str) -> None:
    if node is None or isinstance(node, bool | str):
        return
    if isinstance(node, int):
        return
    if isinstance(node, float):
        raise CanonicalError(f"float at {loc or '<root>'}: hashed documents use integers or strings only")
    if isinstance(node, list | tuple):
        for i, v in enumerate(node):
            _check(v, f"{loc}[{i}]")
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if not isinstance(k, str):
                raise CanonicalError(f"non-string key {k!r} at {loc or '<root>'}")
            _check(v, f"{loc}.{k}" if loc else k)
        return
    raise CanonicalError(f"{type(node).__name__} at {loc or '<root>'} has no canonical JSON form")


def canonical_bytes(doc: Any) -> bytes:
    _check(doc, "")
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def doc_sha256(kind: str, body: Any, **header: str) -> str:
    """Hash of a typed document: the kind and header are part of the hash, so equal bodies of different kinds differ."""
    return sha256_hex(canonical_bytes({"kind": kind, "v": 1, **header, "body": body}))


def pretty_json(doc: Any) -> str:
    """Stable, human-readable JSON for committed files: sorted keys, 2-space indent, LF, trailing newline."""
    _check(doc, "")
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
