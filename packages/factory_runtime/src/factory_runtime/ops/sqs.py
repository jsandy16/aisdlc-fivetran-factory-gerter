"""sqs.read: the message that started the run (ADR-0010).

- `mode: dispatch` (and `s3_event` triggers): the dispatcher already took the message from the queue and wrote the
  custody snapshot; this op reads it, checks that it is intact (body sha256) and still matches the declared fields.
- `mode: in_task` (a scheduled DAG): this op receives one message itself. Invalid messages are quarantined and
  deleted; a message whose run key another run already claimed is a duplicate and is deleted; otherwise the message
  is claimed for this run, snapshotted write-once, and only then deleted. A retry of the task finds the snapshot and
  does not receive again. An empty queue means the run has no work (exit 0; later tasks end at once).
"""

from __future__ import annotations

from typing import Any

from factory_core.canonical import canonical_bytes, sha256_hex
from factory_core.errors import ConfigError, DataError, SecurityError
from factory_core.runkey import run_dir
from factory_core.templates import parse_template
from factory_spec.artifact_models import TriggerSnapshot
from factory_spec.dynamic import record_model
from pydantic import ValidationError

from ..artifacts import TRIGGER_SNAPSHOT
from ..messages import Poison, quarantine, run_key_of, unwrap, validated
from . import NoWork, op

CLAIM = "CLAIM"
WAIT_SECONDS = 5  # long-poll per receive
VISIBILITY_SECONDS = 300  # time to snapshot and delete one message
MAX_RECEIVES = 10  # messages examined per task (skipping duplicates and poison) before giving up for this run


def _intact(ctx: Any, snap: TriggerSnapshot) -> TriggerSnapshot:
    if sha256_hex(canonical_bytes(snap.body)) != snap.body_sha256:
        raise SecurityError("custody_snapshot_altered", "the trigger snapshot does not match its recorded hash")
    trig = ctx.spec.pipeline.trigger
    if trig.type == "sqs":
        try:
            record_model("message", trig.message).model_validate(snap.body)
        except ValidationError:
            raise DataError("message_invalid", "the run's message does not match the declared fields") from None
    return snap


@op("sqs.read")
def sqs_read(ctx: Any) -> list[Any]:
    trig = ctx.spec.pipeline.trigger
    if trig.type not in ("sqs", "s3_event"):
        raise ConfigError("trigger_not_queue", "sqs.read needs an sqs or s3_event trigger")
    snap: TriggerSnapshot | None = ctx.vars.trigger
    if snap is not None:  # written by the dispatcher, or by an earlier attempt of this task
        return [_intact(ctx, snap)]
    if trig.type == "s3_event" or trig.mode == "dispatch":
        raise ConfigError("trigger_snapshot_missing", "the run has no custody snapshot from the dispatcher")
    return [_receive(ctx, trig)]


def _receive(ctx: Any, trig: Any) -> TriggerSnapshot:
    queue = ctx.binding.queues.get(trig.queue)
    if queue is None:
        raise ConfigError("queue_unbound", f"queue '{trig.queue}' is not bound in {ctx.env}")
    sqs = ctx.services.aws.client("sqs")
    url = sqs.get_queue_url(QueueName=queue.name)["QueueUrl"]
    model = record_model("message", trig.message)
    template = parse_template(ctx.spec.pipeline.reliability.run_key)
    state = ctx.services.state
    for _ in range(MAX_RECEIVES):
        msgs = sqs.receive_message(
            QueueUrl=url, MaxNumberOfMessages=1, WaitTimeSeconds=WAIT_SECONDS, VisibilityTimeout=VISIBILITY_SECONDS
        ).get("Messages", [])
        if not msgs:
            raise NoWork("the queue is empty")
        m = msgs[0]
        raw = str(m.get("Body", ""))
        try:
            snap = validated(model, unwrap(raw, trig.envelope), "sqs")
            key = run_key_of(ctx.spec, template, snap)
        except Poison as p:
            quarantine(ctx.services, str(m.get("MessageId", "")), raw, p.code)
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
            continue
        claimed, rec = state.put_if_absent(
            run_dir(key), CLAIM, {"run": ctx.run.run_dir, "body_sha256": snap.body_sha256}
        )
        if not claimed and rec.doc.get("run") != ctx.run.run_dir:
            ctx.log("message_duplicate")
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
            continue
        stored = ctx.staging.put_doc(TRIGGER_SNAPSHOT, snap, adopt=True)
        if stored != snap:  # pragma: no cover - two attempts of one task at once (Airflow prevents it)
            raise ConfigError("trigger_snapshot_conflict", "this run already took another message")
        sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])  # only once the snapshot is durable
        ctx.vars.trigger = snap
        ctx.vars._cache.clear()
        ctx.log("message_received")
        return snap
    raise NoWork("only duplicates or invalid messages were waiting")
