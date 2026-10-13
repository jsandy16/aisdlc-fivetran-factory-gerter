"""Audit events (GxP evidence): write-once JSON lines in the team's audit store (S3 Object Lock in AWS).

One object per task attempt: <pipeline_id>/<run_dir>/<task_id>/attempt-<n>.jsonl. Only the event types listed in
security.yaml `audit.events` are recorded. Events carry ids, hashes, outcomes and timings; never secret values, never
data values, never raw portal payloads.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

from factory_core.canonical import canonical_bytes
from factory_core.jsonlog import redact

from .aws import AwsClients

EVENT_TYPES = frozenset({"delivery", "destination_resolution", "portal_call", "rule_result", "secret_access"})


class AuditLog:
    def __init__(
        self,
        aws: AwsClients | None,
        bucket: str,
        kms_key: str | None,
        prefix: str,
        enabled: frozenset[str],
        *,
        base: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.aws = aws
        self.bucket = bucket
        self.kms_key = kms_key
        self.prefix = prefix
        self.enabled = enabled
        self.base = dict(base or {})
        self.clock = clock
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, event_type: str, **fields: Any) -> None:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown audit event type {event_type}")
        if event_type not in self.enabled:
            return
        doc = {"ts": round(self.clock(), 3), "type": event_type, **self.base, **fields}
        with self._lock:
            self.events.append(doc)

    def _sse(self) -> dict[str, str]:
        return {"ServerSideEncryption": "aws:kms", **({"SSEKMSKeyId": self.kms_key} if self.kms_key else {})}

    def flush(self, name: str) -> str | None:
        """Write the collected events once; returns the object key (None when there was nothing to write)."""
        with self._lock:
            events, self.events = self.events, []
        if not events or self.aws is None:
            return None
        body = "".join(redact(json.dumps(e, sort_keys=True, separators=(",", ":"))) + "\n" for e in events).encode()
        s3 = self.aws.client("s3")
        for n in range(20):
            key = f"{self.prefix}{name}{'' if n == 0 else f'.{n}'}.jsonl"
            try:
                s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/x-ndjson",
                    IfNoneMatch="*",
                    **self._sse(),
                )
                return key
            except Exception as exc:
                if not self.aws.precondition_failed(exc):
                    raise
        raise RuntimeError("audit object names exhausted")  # pragma: no cover

    def put_evidence(self, name: str, doc: Any) -> str | None:
        """Copy a canonical document (e.g. the manifest) to the audit store, write-once."""
        if self.aws is None:
            return None
        key = f"{self.prefix}{name}"
        try:
            self.aws.client("s3").put_object(
                Bucket=self.bucket,
                Key=key,
                Body=canonical_bytes(doc.model_dump(mode="json")),
                ContentType="application/json",
                IfNoneMatch="*",
                **self._sse(),
            )
        except Exception as exc:
            if not self.aws.precondition_failed(exc):
                raise
        return key
