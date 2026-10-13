"""The dispatcher Lambda (ADR-0010, ADR-0014): an SQS batch in; per run key one custody snapshot and one Airflow run.

Per message:
1. unwrap the envelope (raw, sns, eventbridge; S3 event notifications for `s3_event` triggers, one run per record);
2. validate the body against the spec's message fields (errors never echo input) and render the run key;
3. record the dispatch in run-state (`DISP`: received → snapshotted → triggered); a run already triggered makes the
   message a duplicate, which is acknowledged;
4. write the custody snapshot `trigger.json` write-once: the validated body only, so duplicates compare equal;
5. start the DAG run with `dag_run_id = run_dir(run_key)`; conf holds ids, URIs and hashes only; 409 = it exists;
6. write a receipt per message (`MSG#<message id>`): which run it fed and how.
A message that can never succeed (unreadable, invalid, another body under a run key already used) is quarantined
(write-once record in staging, ERROR log for alerting) and acknowledged. Transient failures (run-state, staging, MWAA)
are returned in `batchItemFailures`, so SQS redelivers only those messages; every step above is idempotent.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from factory_core.errors import ConfigError, DataError, ErrorClass, FactoryError, TransientError
from factory_core.jsonlog import bind, configure, get_logger, reset_bound
from factory_core.runkey import run_dir, run_key_hash
from factory_core.templates import parse_template
from factory_spec.artifact_models import TriggerSnapshot
from factory_spec.dynamic import record_model

from .artifacts import TRIGGER_SNAPSHOT
from .bundles import LoadedBundle, load_bundle
from .config import Overrides, from_environ
from .envcheck import check_environment
from .executor import classified
from .messages import Poison, quarantine, run_key_of, s3_objects, snapshot, unwrap, validated
from .services import Services
from .staging import Staging

DISP = "DISP"


class Trigger(Protocol):
    def start(self, dag_id: str, dag_run_id: str, conf: dict[str, Any]) -> Literal["created", "exists"]: ...


class MwaaTrigger:
    """Start a DAG run through MWAA InvokeRestApi (Airflow REST API v2) as the team's trigger role."""

    def __init__(self, client: Any, environment: str) -> None:
        self.client = client
        self.environment = environment

    @classmethod
    def assume(cls, services: Services) -> MwaaTrigger:
        cfg = services.config
        if not cfg.mwaa_environment or not cfg.trigger_role_arn:
            raise ConfigError("dispatcher_not_configured", "the dispatcher has no MWAA environment or trigger role")
        creds = services.aws.client("sts").assume_role(
            RoleArn=cfg.trigger_role_arn, RoleSessionName=f"factory-dispatch-{cfg.pipeline_id}"[:64]
        )["Credentials"]
        client = services.aws.session.client(
            "mwaa",
            region_name=cfg.region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
        return cls(client, cfg.mwaa_environment)

    def start(self, dag_id: str, dag_run_id: str, conf: dict[str, Any]) -> Literal["created", "exists"]:
        body = {"dag_run_id": dag_run_id, "logical_date": None, "conf": conf}
        try:
            res = self.client.invoke_rest_api(
                Name=self.environment, Path=f"/dags/{dag_id}/dagRuns", Method="POST", Body=body
            )
        except Exception as exc:
            r = getattr(exc, "response", None) or {}
            code = str(r.get("Error", {}).get("Code", type(exc).__name__))
            status = int(r.get("RestApiStatusCode") or r.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)
            if status == 409:
                return "exists"
            if code in ("RestApiServerException", "InternalServerException", "ThrottlingException") or status >= 500:
                raise TransientError("trigger_unavailable", f"MWAA could not start the run ({code})") from None
            if status == 429:
                raise TransientError("trigger_unavailable", "MWAA throttled the trigger") from None
            if code == "AccessDeniedException" or status in (401, 403):
                raise ConfigError("trigger_refused", "the trigger role may not start this DAG") from None
            if code == "ResourceNotFoundException" or status == 404:
                raise ConfigError("dag_not_found", f"DAG {dag_id} or MWAA {self.environment} is missing") from None
            if isinstance(exc, FactoryError):
                raise
            if code in ("RestApiClientException", "ValidationException"):
                raise ConfigError("trigger_rejected", f"MWAA rejected the run ({status or code})") from None
            raise
        status = int(res.get("RestApiStatusCode", 0))
        if status == 409:
            return "exists"
        if 200 <= status < 300:
            return "created"
        raise TransientError("trigger_unavailable", f"MWAA answered {status}")


# ---------------------------------------------------------------------------------------------- dispatching


@dataclass
class Dispatched:
    message_id: str
    outcome: Literal["triggered", "duplicate", "quarantined", "ignored", "failed"]
    runs: list[str]
    code: str | None = None


class Dispatcher:
    def __init__(self, services: Services, bundle: LoadedBundle, trigger: Trigger) -> None:
        cfg = services.config
        if not cfg.dag_id:
            raise ConfigError("dispatcher_not_configured", "the dispatcher has no DAG id")
        self.services = services
        self.bundle = bundle
        self.spec = bundle.spec
        self.trigger = trigger
        self.dag_id = cfg.dag_id
        trig = self.spec.pipeline.trigger
        if trig.type not in ("sqs", "s3_event") or (trig.type == "sqs" and trig.mode != "dispatch"):
            raise ConfigError("dispatcher_not_applicable", "this pipeline is not started by the dispatcher")
        self.trig = trig
        self.model = record_model("message", trig.message) if trig.type == "sqs" else None
        self.run_key_template = parse_template(self.spec.pipeline.reliability.run_key)

    def _staging(self, rdir: str) -> Staging:
        cfg = self.services.config
        return Staging(self.services.aws, cfg.staging_bucket, cfg.staging_kms_key, cfg.pipeline_id, rdir)

    def snapshots(self, raw: str) -> list[TriggerSnapshot]:
        if self.trig.type == "s3_event":
            return [snapshot(b, "s3_event") for b in s3_objects(raw, self.trig.key_prefix, self.trig.key_suffix)]
        if self.model is None:  # pragma: no cover - set for every sqs trigger
            raise ConfigError("dispatcher_not_applicable", "no message model")
        return [validated(self.model, unwrap(raw, self.trig.envelope), "sqs")]

    def dispatch_one(self, snap: TriggerSnapshot, message_id: str) -> tuple[str, Literal["triggered", "duplicate"]]:
        state = self.services.state
        key = run_key_of(self.spec, self.run_key_template, snap)
        rdir = run_dir(key)
        with bind(run_key=run_key_hash(key)):
            _, rec = state.put_if_absent(rdir, DISP, {"body_sha256": snap.body_sha256}, state="received")
            if rec.doc.get("body_sha256") != snap.body_sha256:
                raise Poison("run_key_conflict", "another message body already started a run with this run key")
            if rec.state == "triggered":
                self._receipt(rdir, message_id, snap, "duplicate")
                return rdir, "duplicate"
            staging = self._staging(rdir)
            try:
                staging.put_doc(TRIGGER_SNAPSHOT, snap)
            except DataError as exc:
                if exc.code == "staged_artifact_conflict":
                    raise Poison("run_key_conflict", "the run's custody snapshot holds another message body") from None
                raise
            state.transition(rdir, DISP, allowed_from=["received", "snapshotted"], to_state="snapshotted")
            conf = {
                "run_key": key,
                "trigger_snapshot": f"s3://{staging.bucket}/{staging.key(TRIGGER_SNAPSHOT)}",
                "body_sha256": snap.body_sha256,
                "bundle_sha256": self.bundle.sha256,
            }
            started = self.trigger.start(self.dag_id, rdir, conf)
            state.transition(
                rdir, DISP, allowed_from=["snapshotted", "triggered"], to_state="triggered", doc={"dag_run_id": rdir}
            )
            outcome: Literal["triggered", "duplicate"] = "triggered" if started == "created" else "duplicate"
            self._receipt(rdir, message_id, snap, outcome)
            self.services.log.event("run_dispatched", outcome=outcome)
            return rdir, outcome

    def _receipt(self, rdir: str, message_id: str, snap: TriggerSnapshot, outcome: str) -> None:
        sk = f"MSG#{hashlib.sha256(message_id.encode()).hexdigest()[:32]}"
        doc = {"body_sha256": snap.body_sha256, "outcome": outcome, "at": int(self.services.clock())}
        self.services.state.put_if_absent(rdir, sk, doc)

    def handle_record(self, record: Mapping[str, Any]) -> Dispatched:
        message_id = str(record.get("messageId", ""))
        raw = record.get("body", "")
        raw = raw if isinstance(raw, str) else json.dumps(raw)
        try:
            snaps = self.snapshots(raw)
            if not snaps:
                self.services.log.event("message_ignored")
                return Dispatched(message_id, "ignored", [])
            runs: list[str] = []
            outcomes: set[str] = set()
            for snap in snaps:
                rdir, outcome = self.dispatch_one(snap, message_id)
                runs.append(rdir)
                outcomes.add(outcome)
            return Dispatched(message_id, "triggered" if "triggered" in outcomes else "duplicate", runs)
        except Poison as p:
            quarantine(self.services, message_id, raw, p.code)
            return Dispatched(message_id, "quarantined", [], p.code)
        except BaseException as exc:
            err = classified(exc, "dispatch")
            self.services.log.event("dispatch_failed", level="ERROR", error_class=str(err.error_class), code=err.code)
            if err.error_class is ErrorClass.TRANSIENT:
                return Dispatched(message_id, "failed", [], err.code)
            raise err from exc

    def handle_batch(self, event: Mapping[str, Any]) -> tuple[dict[str, Any], list[Dispatched]]:
        """The SQS partial batch response: only transiently failed messages are redelivered."""
        results = [self.handle_record(r) for r in event.get("Records", [])]
        failures = [{"itemIdentifier": r.message_id} for r in results if r.outcome == "failed"]
        return {"batchItemFailures": failures}, results


def build(services: Services, trigger: Trigger | None = None) -> Dispatcher:
    cfg = services.config
    if not cfg.bundle_uri or not cfg.bundle_sha256:
        raise ConfigError("dispatcher_not_configured", "the dispatcher has no bundle")
    bundle = load_bundle(services.aws, cfg, cfg.bundle_uri, cfg.bundle_sha256)
    return Dispatcher(services, bundle, trigger or MwaaTrigger.assume(services))


_configured = False


def handle(
    event: dict[str, Any],
    context: Any = None,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Overrides | None = None,
    trigger_factory: Callable[[Services], Trigger] | None = None,
) -> dict[str, Any]:
    """Lambda entry point for the SQS event source mapping (ReportBatchItemFailures enabled).

    A configuration or security failure fails the whole batch (raised: every message is retried and alerts fire);
    a per-message transient failure is reported for that message only.
    """
    global _configured
    if not _configured and environ is None:
        configure(sys.stdout, service="factory-dispatcher")
        _configured = True
    reset_bound()
    env = os.environ if environ is None else environ
    cfg = from_environ(env)
    check_environment(cfg.env, env)
    services = Services.build(cfg, overrides)
    with bind(pipeline_id=cfg.pipeline_id, team=cfg.team, env=cfg.env):
        dispatcher = build(services, trigger_factory(services) if trigger_factory else None)
        response, results = dispatcher.handle_batch(event)
        get_logger().event(
            "batch_dispatched",
            messages=len(results),
            failed=len(response["batchItemFailures"]),
            quarantined=sum(1 for r in results if r.outcome == "quarantined"),
        )
        return response
