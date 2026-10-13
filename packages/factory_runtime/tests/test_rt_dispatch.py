"""The dispatcher Lambda and sqs.read (ADR-0010, ADR-0014, R3): custody snapshot, one run per run key, quarantine.

AWS is moto in-process; the MWAA call is a recording fake (the boto3 implementation is tested with botocore's
Stubber). Synthetic messages only.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from factory_core.errors import ConfigError, SecurityError, TransientError
from factory_core.runkey import run_dir
from factory_runtime.artifacts import TRIGGER_SNAPSHOT
from factory_runtime.config import RuntimeConfig
from factory_runtime.messages import Poison, s3_objects, unwrap
from factory_runtime.ops import NoWork
from factory_runtime.services import Services
from factory_runtime.staging import Staging
from factory_runtime.state.dynamodb import DynamoRunState
from factory_spec.artifact_models import TriggerSnapshot
from rt_helpers import (
    EXAMPLE_REPO,
    LogCapture,
    OpHarness,
    copy_repo,
    make_config,
    publish_bundle,
    run_task,
    setup_stores,
)

from factory_core import yamlio
from factory_runtime import dispatch

REGION = "eu-west-1"
DAG = "study_pkg_delivery"


def message(request_id: str = "REQ-00000001", **kw: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "request_id": request_id,
        "study_id": "ONC101",
        "package_id": "PKG0042",
        "bucket": "acme-clinops-landing-dev",
        "prefix": "incoming/ONC101/PKG0042/",
        "file_count": 3,
        "total_bytes": 1200,
    }
    msg.update(kw)
    return msg


def record(body: Any, message_id: str) -> dict[str, Any]:
    return {"messageId": message_id, "receiptHandle": f"rh-{message_id}",
            "body": body if isinstance(body, str) else json.dumps(body)}  # fmt: skip


class FakeTrigger:
    """Records started runs; a second start of the same dag_run_id answers like Airflow's 409."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.calls = 0
        self.fail: set[str] = set()
        self._lock = threading.Lock()

    def start(self, dag_id: str, dag_run_id: str, conf: dict[str, Any]) -> Any:
        with self._lock:
            self.calls += 1
            if dag_run_id in self.fail:
                raise TransientError("trigger_unavailable", "MWAA is unavailable")
            if dag_run_id in self.runs:
                return "exists"
            self.runs[dag_run_id] = conf
            return "created"


def _relock(pdir: Path) -> None:
    from factory_spec.load import load_spec
    from factory_spec.lock import write_lock

    spec = load_spec(pdir).spec
    assert spec is not None
    write_lock(spec)


@pytest.fixture
def example(tmp_path: Path, workdir: Path, aws: None) -> dict[str, Any]:
    repo = copy_repo(EXAMPLE_REPO, tmp_path / "repo")
    pdir = repo / "pipelines" / "study_pkg_delivery"
    base = make_config(workdir, "study_pkg_delivery")
    setup_stores(base)
    return {"pdir": pdir, "base": base}


def _dispatcher(example: dict[str, Any]) -> tuple[dispatch.Dispatcher, FakeTrigger, RuntimeConfig]:
    uri, sha = publish_bundle(example["base"], example["pdir"])
    cfg = example["base"].model_copy(update={"bundle_uri": uri, "bundle_sha256": sha, "dag_id": DAG})
    trigger = FakeTrigger()
    return dispatch.build(Services.build(cfg), trigger), trigger, cfg


def _state(cfg: RuntimeConfig) -> DynamoRunState:
    return DynamoRunState(boto3.client("dynamodb", region_name=REGION), cfg.run_state_table, cfg.pipeline_id)


def _staged(cfg: RuntimeConfig, rdir: str) -> Staging:
    from factory_runtime.aws import AwsClients

    return Staging(AwsClients(cfg, None), cfg.staging_bucket, None, cfg.pipeline_id, rdir)


def test_dispatch_snapshots_triggers_and_records(example: dict[str, Any]) -> None:
    with LogCapture() as logs:
        d, trigger, cfg = _dispatcher(example)
        response, results = d.handle_batch({"Records": [record(message(note="not declared"), "m-1")]})
    assert response == {"batchItemFailures": []}
    assert [(r.outcome, r.runs) for r in results] == [("triggered", [run_dir("REQ-00000001")])]
    rdir = run_dir("REQ-00000001")
    snap = _staged(cfg, rdir).get_doc(TRIGGER_SNAPSHOT, TriggerSnapshot)
    assert snap.body == message()  # validated body only: the undeclared field is not kept
    conf = trigger.runs[rdir]
    assert sorted(conf) == ["body_sha256", "bundle_sha256", "run_key", "trigger_snapshot"]
    assert conf["body_sha256"] == snap.body_sha256
    assert "ONC101" not in json.dumps(conf)  # ids, URIs and hashes only: no message content in the Airflow conf
    st = _state(cfg)
    disp = st.get(rdir, "DISP")
    assert disp is not None
    assert (disp.state, disp.doc["dag_run_id"]) == ("triggered", rdir)
    assert "ONC101" not in logs.text
    assert "PKG0042" not in logs.text


def test_partial_batch_duplicates_and_quarantine(example: dict[str, Any]) -> None:
    with LogCapture() as logs:
        d, trigger, cfg = _dispatcher(example)
    trigger.fail.add(run_dir("REQ-00000006"))
    batch = {
        "Records": [
            record(message(), "m-1"),
            record(message(), "m-2"),  # the same message delivered twice (standard queue)
            record(message(study_id=None), "m-3"),  # not valid against the declared fields
            record("{not json", "m-4"),
            record(message(study_id="ONC999"), "m-5"),  # another body under a run key already used
            record(message("REQ-00000006"), "m-6"),  # MWAA unavailable: only this one is redelivered
        ]
    }
    with logs:
        response, results = d.handle_batch(batch)
    assert response == {"batchItemFailures": [{"itemIdentifier": "m-6"}]}
    assert [(r.message_id, r.outcome, r.code) for r in results] == [
        ("m-1", "triggered", None),
        ("m-2", "duplicate", None),
        ("m-3", "quarantined", "message_invalid"),
        ("m-4", "quarantined", "message_unreadable"),
        ("m-5", "quarantined", "run_key_conflict"),
        ("m-6", "failed", "trigger_unavailable"),
    ]
    assert list(trigger.runs) == [run_dir("REQ-00000001")]
    q = boto3.client("s3", region_name=REGION).list_objects_v2(
        Bucket=cfg.staging_bucket, Prefix=f"{cfg.pipeline_id}/_quarantine/"
    )
    assert q["KeyCount"] == 3
    assert [e["code"] for e in logs.events("message_quarantined")] == [
        "message_invalid",
        "message_unreadable",
        "run_key_conflict",
    ]
    assert "ONC999" not in logs.text  # a quarantined body is kept in custody, never logged
    # the redelivered message succeeds once MWAA is back; the snapshot written the first time is reused
    trigger.fail.clear()
    response, results = d.handle_batch({"Records": [record(message("REQ-00000006"), "m-6")]})
    assert response == {"batchItemFailures": []}
    assert results[0].outcome == "triggered"


def test_concurrent_duplicates_start_one_run(example: dict[str, Any]) -> None:
    """R3: ten copies of one message processed at once start exactly one run."""
    d, trigger, cfg = _dispatcher(example)
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda i: d.handle_record(record(message(), f"m-{i}")), range(10)))
    assert len(trigger.runs) == 1
    assert sorted(r.outcome for r in results) == ["duplicate"] * 9 + ["triggered"]
    assert all(r.runs == [run_dir("REQ-00000001")] for r in results)
    disp = _state(cfg).get(run_dir("REQ-00000001"), "DISP")
    assert disp is not None
    assert disp.state == "triggered"


def test_envelopes_and_s3_events() -> None:
    body = json.dumps(message())
    assert unwrap(body, "raw") == message()
    sns = json.dumps({"Type": "Notification", "MessageId": "x", "Message": body})
    assert unwrap(sns, "sns") == message()
    assert unwrap(json.dumps({"detail-type": "Package ready", "detail": message()}), "eventbridge") == message()
    for bad, env in ((body, "sns"), (json.dumps({"Type": "Notification", "Message": "[1]"}), "sns"), ("[]", "raw")):
        with pytest.raises(Poison):
            unwrap(bad, env)
    event = {
        "Records": [
            {"eventName": "ObjectCreated:Put", "s3": {"bucket": {"name": "src"},
             "object": {"key": "incoming/a+b%2Bc.csv", "size": 5, "sequencer": "01", "versionId": "v1"}}},
            {"eventName": "ObjectCreated:Put", "s3": {"bucket": {"name": "src"},
             "object": {"key": "other/x.csv", "size": 1, "sequencer": "02"}}},
            {"eventName": "ObjectRemoved:Delete", "s3": {"bucket": {"name": "src"},
             "object": {"key": "incoming/gone.csv", "sequencer": "03"}}},
        ]
    }  # fmt: skip
    assert s3_objects(json.dumps(event), "incoming/", None) == [
        {"bucket": "src", "key": "incoming/a b+c.csv", "size": 5, "sequencer": "01", "version_id": "v1"}
    ]
    assert s3_objects(json.dumps({"Event": "s3:TestEvent"}), None, None) == []
    with pytest.raises(Poison):
        s3_objects(json.dumps({"Records": [{"eventName": "ObjectCreated:Put"}]}), None, None)


def test_mwaa_trigger_maps_answers() -> None:
    client = boto3.client("mwaa", region_name=REGION, aws_access_key_id="testing", aws_secret_access_key="testing")
    t = dispatch.MwaaTrigger(client, "factory-dev")
    expected = {
        "Name": "factory-dev",
        "Path": f"/dags/{DAG}/dagRuns",
        "Method": "POST",
        "Body": {"dag_run_id": "run-1", "logical_date": None, "conf": {"run_key": "REQ-1"}},
    }
    with Stubber(client) as stub:
        stub.add_response("invoke_rest_api", {"RestApiStatusCode": 200, "RestApiResponse": {}}, expected)
        stub.add_client_error("invoke_rest_api", "RestApiClientException", http_status_code=400,
                              modeled_fields={"RestApiStatusCode": 409, "RestApiResponse": {}})  # fmt: skip
        stub.add_client_error("invoke_rest_api", "RestApiServerException", http_status_code=400,
                              modeled_fields={"RestApiStatusCode": 503, "RestApiResponse": {}})  # fmt: skip
        stub.add_client_error("invoke_rest_api", "AccessDeniedException", http_status_code=403)
        stub.add_client_error("invoke_rest_api", "ResourceNotFoundException", http_status_code=404)
        stub.add_client_error("invoke_rest_api", "RestApiClientException", http_status_code=400,
                              modeled_fields={"RestApiStatusCode": 400, "RestApiResponse": {}})  # fmt: skip
        assert t.start(DAG, "run-1", {"run_key": "REQ-1"}) == "created"
        assert t.start(DAG, "run-1", {"run_key": "REQ-1"}) == "exists"
        with pytest.raises(TransientError):
            t.start(DAG, "run-1", {"run_key": "REQ-1"})
        for code in ("trigger_refused", "dag_not_found", "trigger_rejected"):
            with pytest.raises(ConfigError) as ei:
                t.start(DAG, "run-1", {"run_key": "REQ-1"})
            assert ei.value.code == code


def test_lambda_entry_reads_its_configuration(example: dict[str, Any], workdir: Path) -> None:
    uri, sha = publish_bundle(example["base"], example["pdir"])
    cfg = example["base"]
    environ = {
        "FACTORY_ENV": "dev",
        "FACTORY_TEAM": cfg.team,
        "FACTORY_PIPELINE_ID": cfg.pipeline_id,
        "FACTORY_REGION": REGION,
        "FACTORY_STAGING_BUCKET": cfg.staging_bucket,
        "FACTORY_RUN_STATE_TABLE": cfg.run_state_table,
        "FACTORY_BUNDLE_STORE": cfg.bundle_store,
        "FACTORY_AUDIT_STORE": cfg.audit_store,
        "FACTORY_BUNDLE_URI": uri,
        "FACTORY_BUNDLE_SHA256": sha,
        "FACTORY_DAG_ID": DAG,
        "FACTORY_WORKDIR": str(workdir),
    }
    trigger = FakeTrigger()
    out = dispatch.handle({"Records": [record(message(), "m-1")]}, None, environ=environ,
                          trigger_factory=lambda s: trigger)  # fmt: skip
    assert out == {"batchItemFailures": []}
    assert list(trigger.runs) == [run_dir("REQ-00000001")]
    with pytest.raises(ConfigError) as ei:  # a configuration problem fails the whole batch (all retried, alerted)
        dispatch.handle({"Records": []}, None, environ={**environ, "FACTORY_DAG_ID": ""},
                        trigger_factory=lambda s: trigger)  # fmt: skip
    assert ei.value.code == "dispatcher_not_configured"


# ---------------------------------------------------------------- sqs.read


def test_sqs_read_in_dispatch_mode_checks_the_snapshot(example: dict[str, Any], workdir: Path) -> None:
    h = OpHarness(example["pdir"], example["base"], trigger_body=message())
    assert h.run_rule("r_read_request")[0].body == message()
    h.vars.trigger = TriggerSnapshot(source="sqs", body=message(study_id="ONC999"), body_sha256="0" * 64)
    with pytest.raises(SecurityError) as ei:
        h.run_rule("r_read_request")
    assert ei.value.code == "custody_snapshot_altered"
    h.vars.trigger = None
    with pytest.raises(ConfigError) as ce:
        h.run_rule("r_read_request")
    assert ce.value.code == "trigger_snapshot_missing"


@pytest.fixture
def in_task(example: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from factory_runtime.ops import sqs as sqs_op

    monkeypatch.setattr(sqs_op, "WAIT_SECONDS", 0)
    f = example["pdir"] / "spec" / "pipeline.yaml"
    doc = yamlio.load_path(f)
    doc["trigger"]["mode"] = "in_task"
    yamlio.dump_path(doc, f)
    _relock(example["pdir"])
    sqs = boto3.client("sqs", region_name=REGION)
    url = sqs.create_queue(QueueName="clinops-package-ready-dev")["QueueUrl"]
    return {**example, "sqs": sqs, "url": url}


def _queue_len(env: dict[str, Any]) -> int:
    attrs = env["sqs"].get_queue_attributes(QueueUrl=env["url"], AttributeNames=["All"])["Attributes"]
    return int(attrs["ApproximateNumberOfMessages"]) + int(attrs["ApproximateNumberOfMessagesNotVisible"])


def test_sqs_read_in_task_claims_snapshots_then_deletes(in_task: dict[str, Any]) -> None:
    sqs, url = in_task["sqs"], in_task["url"]
    sqs.send_message(QueueUrl=url, MessageBody="{broken")  # poison: quarantined and deleted
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps(message()))
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps(message()))  # its duplicate
    h = OpHarness(in_task["pdir"], in_task["base"], run_key="scheduled__2026-10-13T06:00:00")
    snap = h.run_rule("r_read_request")[0]
    assert snap.body == message()
    assert h.staging.get_doc(TRIGGER_SNAPSHOT, TriggerSnapshot) == snap
    assert h.vars.get("study_id") == "ONC101"  # later rules of the run see the message
    assert h.run_rule("r_read_request")[0] == snap  # a retry does not receive again
    # another scheduled run finds only the duplicate (claimed by the first run): deleted, then no work
    h2 = OpHarness(in_task["pdir"], in_task["base"], run_key="scheduled__2026-10-13T06:15:00")
    h2.services.state = h.services.state  # one run-state table for every run of the pipeline
    with pytest.raises(NoWork):
        h2.run_rule("r_read_request")
    assert _queue_len(in_task) == 0


def test_empty_queue_ends_the_run_without_error(in_task: dict[str, Any]) -> None:
    cfg = in_task["base"]
    uri, sha = publish_bundle(cfg, in_task["pdir"])
    key = "scheduled__2026-10-13T06:30:00"
    with LogCapture() as logs:
        assert run_task(cfg, uri, sha, "t1_collect", key) == 0
        assert run_task(cfg, uri, sha, "t2_checksum", key) == 0  # later tasks end at once
    assert [e["status"] for e in logs.events("run_exit")] == ["no_work", "no_work"]
    assert _state(cfg).get(run_dir(key), "NOWORK") is not None
