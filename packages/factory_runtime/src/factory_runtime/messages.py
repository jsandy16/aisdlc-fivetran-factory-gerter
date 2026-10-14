"""Queue messages: unwrapping envelopes, validating bodies, run keys, and quarantining what can never succeed.

Shared by the dispatcher Lambda and the in_task mode of sqs.read. Validation errors never echo the input, and a
message body is never logged: only its sha256.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import unquote_plus

from factory_core.canonical import canonical_bytes, sha256_hex
from factory_core.templates import Template, TemplateError, render
from factory_spec.artifact_models import TriggerSnapshot
from pydantic import BaseModel, ValidationError

from .artifacts import Vars
from .services import Services
from .staging import Staging

QUARANTINE_DIR = "_quarantine"
MAX_BODY = 256 * 1024  # the SQS maximum


class Poison(Exception):  # noqa: N818 - a message that can never succeed
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _json(text: Any, what: str) -> Any:
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str) or len(text.encode()) > MAX_BODY:
        raise Poison("message_unreadable", f"{what} is not text or is too large")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise Poison("message_unreadable", f"{what} is not JSON") from None


def unwrap(body: str, envelope: str) -> dict[str, Any]:
    """The message itself, from its envelope (raw, sns, eventbridge)."""
    doc = _json(body, "the message")
    if envelope == "sns":
        if not isinstance(doc, dict) or doc.get("Type") != "Notification":
            raise Poison("envelope_invalid", "not an SNS notification")
        doc = _json(doc.get("Message"), "the SNS message")
    elif envelope == "eventbridge":
        if not isinstance(doc, dict) or "detail" not in doc:
            raise Poison("envelope_invalid", "not an EventBridge event")
        doc = _json(doc["detail"], "the event detail")
    if not isinstance(doc, dict):
        raise Poison("message_invalid", "the message is not a JSON object")
    return doc


def s3_objects(body: str, key_prefix: str | None, key_suffix: str | None) -> list[dict[str, Any]]:
    """S3 event notification → one body per matching created object (bucket, key, size, version_id, sequencer)."""
    doc = _json(body, "the S3 event")
    if isinstance(doc, dict) and doc.get("Event") == "s3:TestEvent":
        return []
    records = doc.get("Records") if isinstance(doc, dict) else None
    if not isinstance(records, list):
        raise Poison("message_invalid", "not an S3 event notification")
    out: list[dict[str, Any]] = []
    for rec in records:
        try:
            if not str(rec["eventName"]).startswith("ObjectCreated:"):
                continue
            s3 = rec["s3"]
            obj: dict[str, Any] = {
                "bucket": str(s3["bucket"]["name"]),
                "key": unquote_plus(str(s3["object"]["key"])),
                "size": int(s3["object"].get("size", 0)),
                "sequencer": str(s3["object"]["sequencer"]),
            }
        except (KeyError, TypeError, ValueError):
            raise Poison("message_invalid", "an S3 event record is incomplete") from None
        if s3["object"].get("versionId"):
            obj["version_id"] = str(s3["object"]["versionId"])
        if key_prefix and not obj["key"].startswith(key_prefix):
            continue
        if key_suffix and not obj["key"].endswith(key_suffix):
            continue
        out.append(obj)
    return out


def quarantine(services: Services, message_id: str, raw: str, code: str) -> None:
    """Keep a message that can never succeed (write-once, in the custody store) and alert on it; never log its body."""
    cfg = services.config
    doc = {
        "message_id": message_id,
        "code": code,
        "received_at": int(services.clock()),
        "body_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "body": raw,
    }
    name = f"{hashlib.sha256(message_id.encode()).hexdigest()[:32]}.json"
    Staging(services.aws, cfg.staging_bucket, cfg.staging_kms_key, cfg.staging_root, QUARANTINE_DIR).put_once(
        name, canonical_bytes(doc)
    )
    services.log.event("message_quarantined", level="ERROR", code=code, body_sha256=doc["body_sha256"])


def validated(model: type[BaseModel], doc: dict[str, Any], source: str) -> TriggerSnapshot:
    """The custody snapshot of a message: its validated body only, with the body's canonical sha256."""
    try:
        body = model.model_validate(doc).model_dump(mode="json", by_alias=True, exclude_none=True)
    except ValidationError as exc:
        fields = sorted({".".join(map(str, e["loc"])) for e in exc.errors(include_input=False)})
        raise Poison("message_invalid", f"the message does not match the declared fields: {fields}") from None
    return snapshot(body, source)


def snapshot(body: dict[str, Any], source: str) -> TriggerSnapshot:
    return TriggerSnapshot(
        source=source,  # type: ignore[arg-type]
        body=body,
        body_sha256=sha256_hex(canonical_bytes(body)),
    )


def run_key_of(spec: Any, template: Template, snap: TriggerSnapshot) -> str:
    values = Vars(spec, snap, lambda _: None).strings()
    try:
        key = render(template, values)
    except TemplateError:
        raise Poison("run_key_unavailable", "the message lacks a field the run key needs") from None
    if not key or len(key) > 1024:
        raise Poison("run_key_unavailable", "the run key is empty or too long")
    return key
