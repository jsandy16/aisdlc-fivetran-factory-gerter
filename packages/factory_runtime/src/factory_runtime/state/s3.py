"""Run-state as S3 objects (the dataops layout, ADR-0026): one JSON object per (run, record), no DynamoDB.

Same semantics as the DynamoDB store. Every guarded write is an S3 conditional write: `If-None-Match: *` creates a
record only once, `If-Match: <etag>` replaces only the version that was read (compare-and-set). A lost race re-reads
and decides again, so concurrent containers never both win. Keys: <root><run_dir>/<record>.json; sequence counters
under <root>_seq/<name>/.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from factory_core.canonical import canonical_bytes

from .protocol import Record

_ATTEMPTS = 20


def _code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    return str(response.get("Error", {}).get("Code", "")) if isinstance(response, dict) else ""


def _lost_race(exc: BaseException) -> bool:
    return _code(exc) in ("PreconditionFailed", "ConditionalRequestConflict", "412")


def _missing(exc: BaseException) -> bool:
    return _code(exc) in ("NoSuchKey", "404", "NotFound")


class S3RunState:
    def __init__(self, client: Any, bucket: str, root: str) -> None:
        self.client = client
        self.bucket = bucket
        self.root = root

    def _key(self, run_dir: str, sk: str) -> str:
        return f"{self.root}{run_dir}/{quote(sk, safe='')}.json"

    def _read(self, key: str) -> tuple[Record | None, str | None]:
        try:
            res = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _missing(exc):
                return None, None
            raise
        item = json.loads(res["Body"].read())
        return Record(item["sk"], item.get("doc", {}), item.get("state")), str(res["ETag"])

    def _body(self, sk: str, doc: dict[str, Any], state: str | None) -> bytes:
        item: dict[str, Any] = {"sk": sk, "doc": doc}
        if state is not None:
            item["state"] = state
        return canonical_bytes(item)

    def _write(self, key: str, body: bytes, **cond: str) -> bool:
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json", **cond)
            return True
        except Exception as exc:
            if _lost_race(exc) or (cond.get("IfMatch") and _missing(exc)):
                return False
            raise

    def get(self, run_dir: str, sk: str) -> Record | None:
        return self._read(self._key(run_dir, sk))[0]

    def put(self, run_dir: str, sk: str, doc: dict[str, Any], *, state: str | None = None) -> None:
        self._write(self._key(run_dir, sk), self._body(sk, doc, state))

    def put_if_absent(
        self, run_dir: str, sk: str, doc: dict[str, Any], *, state: str | None = None
    ) -> tuple[bool, Record]:
        key = self._key(run_dir, sk)
        for _ in range(_ATTEMPTS):
            if self._write(key, self._body(sk, doc, state), IfNoneMatch="*"):
                return True, Record(sk, dict(doc), state)
            existing, _ = self._read(key)
            if existing is not None:
                return False, existing
        raise RuntimeError("run-state record kept changing during a conditional write")  # pragma: no cover

    def transition(
        self,
        run_dir: str,
        sk: str,
        *,
        allowed_from: Iterable[str | None],
        to_state: str,
        doc: dict[str, Any] | None = None,
    ) -> bool:
        key = self._key(run_dir, sk)
        allowed = set(allowed_from)
        for _ in range(_ATTEMPTS):
            current, etag = self._read(key)
            if (current.state if current else None) not in allowed:
                return False
            merged = {**(current.doc if current else {}), **(doc or {})}
            cond = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
            if self._write(key, self._body(sk, merged, to_state), **cond):
                return True
        raise RuntimeError("run-state record kept changing during a transition")  # pragma: no cover

    def allocate_seq(self, name: str, run_dir: str) -> int:
        """The run's number in the named sequence; retries get the same number. A lost race leaves a gap."""
        mine = f"{self.root}_seq/{quote(name, safe='')}/{run_dir}.json"
        existing, _ = self._read(mine)
        if existing is not None:
            return int(existing.doc["n"])
        counter = f"{self.root}_seq/{quote(name, safe='')}/COUNTER.json"
        for _ in range(_ATTEMPTS):
            current, etag = self._read(counter)
            n = (int(current.doc["n"]) if current else 0) + 1
            cond = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
            if self._write(counter, self._body("COUNTER", {"n": n}, None), **cond):
                break
        else:  # pragma: no cover
            raise RuntimeError("sequence counter kept changing")
        if self._write(mine, self._body(run_dir, {"n": n}, None), IfNoneMatch="*"):
            return n
        winner, _ = self._read(mine)
        if winner is None:  # pragma: no cover
            raise RuntimeError("sequence record vanished during a conditional write")
        return int(winner.doc["n"])
