"""The dataops layout runtime (ADR-0026): S3 run state, the container entry point, the DataOps Vault provider.

Synthetic data only; AWS is in-process moto.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest
from factory_core.errors import ConfigError, SecurityError, TransientError
from factory_runtime.config import RuntimeConfig
from factory_runtime.container import BUNDLE_FILE, Pipeline, Run
from factory_runtime.envcheck import check_environment
from factory_runtime.secrets.dataops_vault import DataopsVaultProvider, project_entity
from factory_runtime.secrets.provider import ResolvedRef
from factory_runtime.services import Services
from factory_runtime.state.s3 import S3RunState
from factory_spec.bundle import build_bundle
from rt_helpers import REGION, UNIT_REPO, LogCapture, copy_repo

from factory_core import jsonlog

STATE = "unit-factory-state-dev"
QUEUE = "unit-object-created-dev"


def _bucket(name: str) -> None:
    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION}
    )


# ---------------------------------------------------------------- S3 run state


def test_s3_run_state_has_the_dynamodb_semantics(aws: None) -> None:
    _bucket(STATE)
    st = S3RunState(boto3.client("s3", region_name=REGION), STATE, "_factory/state/p/")
    assert st.get("r1", "TASK#t1") is None
    assert st.put_if_absent("r1", "DISP", {"b": 1}, state="received")[0] is True
    won, rec = st.put_if_absent("r1", "DISP", {"b": 2}, state="received")
    assert (won, rec.doc, rec.state) == (False, {"b": 1}, "received")
    assert st.transition("r1", "DISP", allowed_from=["snapshotted"], to_state="triggered") is False
    assert st.transition("r1", "DISP", allowed_from=["received"], to_state="snapshotted", doc={"x": 1}) is True
    rec2 = st.get("r1", "DISP")
    assert rec2 is not None
    assert (rec2.state, rec2.doc) == ("snapshotted", {"b": 1, "x": 1})
    assert st.transition("r1", "NEW", allowed_from=[None], to_state="a") is True  # absent → created
    assert [st.allocate_seq("pkg", r) for r in ("r1", "r2", "r1", "r3")] == [1, 2, 1, 3]
    keys = [o["Key"] for o in boto3.client("s3", region_name=REGION).list_objects_v2(Bucket=STATE)["Contents"]]
    assert "_factory/state/p/r1/TASK%23t1.json" not in keys
    assert "_factory/state/p/r1/DISP.json" in keys


def test_s3_run_state_compare_and_set_has_one_winner(aws: None) -> None:
    _bucket(STATE)
    st = S3RunState(boto3.client("s3", region_name=REGION), STATE, "s/")
    st.put("r", "DISP", {}, state="received")
    wins: list[bool] = []
    barrier = threading.Barrier(8)

    def contend(i: int) -> None:
        barrier.wait()
        wins.append(st.transition("r", "DISP", allowed_from=["received"], to_state=f"by-{i}"))

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1


# ---------------------------------------------------------------- the container entry point


def _container_dir(tmp_path: Path, visibility: str = "30", **runtime: Any) -> tuple[Path, str]:
    repo = copy_repo(UNIT_REPO, tmp_path / "repo", environments=["dev"])
    bundle = build_bundle(repo / "pipelines" / "unit_s3_event")
    d = tmp_path / "src" / "unit_s3_event"
    d.mkdir(parents=True)
    (d / BUNDLE_FILE).write_bytes(bundle.data)
    queue_url = boto3.client("sqs", region_name=REGION).create_queue(
        QueueName=QUEUE, Attributes={"VisibilityTimeout": visibility}
    )["QueueUrl"]
    cfg = {"team": "clinops_data", "pipeline_id": "unit_s3_event", "region": REGION, "staging_bucket": STATE,
           "audit_store": STATE, "bundle_store": STATE, "state_store": "s3", "store_prefix": "_factory/",
           "dag_id": "unit-s3-event", "image_digest": "container", "workdir": str(tmp_path / "work"),
           **runtime}  # fmt: skip
    doc = {"environment": "dev", "runtime": cfg, "poll": {"queue_url": queue_url, "wait_seconds": 0}}
    (d / "config_dev.json").write_text(json.dumps(doc))
    return d, bundle.sha256


def _event(version_id: str, key: str = "incoming/batch+7/a.csv") -> str:
    return json.dumps({"Records": [{"eventName": "ObjectCreated:Put", "s3": {"bucket": {"name": "unit-source-dev"},
                       "object": {"key": key, "size": 5, "sequencer": "0A1", "versionId": version_id}}}]})  # fmt: skip


def _world() -> tuple[Any, str]:
    _bucket(STATE)
    for b in ("unit-source-dev", "unit-archive-dev"):
        _bucket(b)
    s3 = boto3.client("s3", region_name=REGION)
    s3.put_bucket_versioning(Bucket="unit-source-dev", VersioningConfiguration={"Status": "Enabled"})
    put = s3.put_object(Bucket="unit-source-dev", Key="incoming/batch 7/a.csv", Body=b"id\n1\n")
    return s3, put["VersionId"]


def copy_new_object(run: Run) -> None:  # what the generated task.py writes for unit_s3_event
    with run.task("t1_copy") as task:
        task.step("r_read_event")
        task.step("r_list_object")
        task.step("r_copy")


def test_container_polls_copies_and_acknowledges(tmp_path: Path, aws: None) -> None:
    s3, version = _world()
    d, sha = _container_dir(tmp_path)
    sqs = boto3.client("sqs", region_name=REGION)
    url = sqs.get_queue_url(QueueName=QUEUE)["QueueUrl"]
    for body in (_event(version), _event(version), "not json"):  # the run, its duplicate, a poison message
        sqs.send_message(QueueUrl=url, MessageBody=body)
    with LogCapture() as logs:
        pipeline = Pipeline.load(d, sha, environ={"project_env": "dev"}, overrides=_overrides())
        assert pipeline.run_each(copy_new_object) == 0
    assert s3.get_object(Bucket="unit-archive-dev", Key="copies/dev/a.csv")["Body"].read() == b"id\n1\n"
    assert sqs.receive_message(QueueUrl=url, WaitTimeSeconds=0).get("Messages") is None  # all three acknowledged
    assert len(logs.events("task_end")) == 1  # the duplicate did not run again
    assert len(logs.events("message_quarantined")) == 1
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=STATE)["Contents"]}
    assert any(k.startswith("_factory/bundles/clinops_data/unit_s3_event/") for k in keys)  # published for pinning
    assert any(k.startswith("_factory/staging/unit_s3_event/_quarantine/") for k in keys)
    assert any(k.startswith("_factory/audit/unit_s3_event/") for k in keys)


def test_a_failed_run_keeps_its_message_and_sets_the_exit_code(tmp_path: Path, aws: None) -> None:
    _, version = _world()
    d, sha = _container_dir(tmp_path, visibility="0")
    sqs = boto3.client("sqs", region_name=REGION)
    url = sqs.get_queue_url(QueueName=QUEUE)["QueueUrl"]
    sqs.send_message(QueueUrl=url, MessageBody=_event(version))

    def out_of_order(run: Run) -> None:
        with run.task("t1_copy") as task:
            task.step("r_copy")

    pipeline = Pipeline.load(d, sha, environ={"project_env": "dev"}, overrides=_overrides())
    assert pipeline.run_each(out_of_order) == 78
    msgs = sqs.receive_message(QueueUrl=url, WaitTimeSeconds=0).get("Messages")
    assert msgs is not None
    assert len(msgs) == 1
    pipeline = Pipeline.load(d, sha, environ={"project_env": "dev"}, overrides=_overrides())
    assert pipeline.run_each(copy_new_object) == 0  # the corrected code finishes the same run


def test_container_refuses_a_tampered_bundle_and_a_missing_environment(tmp_path: Path, aws: None) -> None:
    _world()
    d, sha = _container_dir(tmp_path)
    with pytest.raises(ConfigError, match="project_env"):
        Pipeline.load(d, sha, environ={}, overrides=_overrides())
    with pytest.raises(ConfigError, match=r"config_prod\.json"):
        Pipeline.load(d, sha, environ={"project_env": "prod"}, overrides=_overrides())
    data = bytearray((d / BUNDLE_FILE).read_bytes())
    data[100] ^= 1
    (d / BUNDLE_FILE).write_bytes(bytes(data))
    with pytest.raises(SecurityError):
        Pipeline.load(d, sha, environ={"project_env": "dev"}, overrides=_overrides())


def _overrides() -> Any:
    from factory_runtime.config import Overrides

    return Overrides()


# ---------------------------------------------------------------- image variables in strict environments


def test_only_the_declared_proxy_and_ca_are_accepted_in_prd() -> None:
    allowed = {"HTTPS_PROXY": "http://proxy.corp.example:9480", "SSL_CERT_FILE": "/etc/pki/tls/certs/ca-bundle.crt"}
    check_environment("prd", dict(allowed), allowed)
    with pytest.raises(SecurityError, match="HTTPS_PROXY"):
        check_environment("prd", {**allowed, "HTTPS_PROXY": "http://elsewhere.example:3128"}, allowed)
    with pytest.raises(SecurityError, match="AWS_ENDPOINT_URL"):
        check_environment("prd", {**allowed, "AWS_ENDPOINT_URL": "http://x"}, allowed)
    check_environment("dev", {"HTTPS_PROXY": "anything"})


# ---------------------------------------------------------------- the DataOps Vault provider

TOKEN = "s.synthetic-token-0001"


def _vault(handler: Any) -> tuple[DataopsVaultProvider, Services]:
    cfg = RuntimeConfig.model_validate({"env": "dev", "team": "t", "pipeline_id": "p", "region": "eu-north-1",
                                        "staging_bucket": STATE, "audit_store": STATE, "bundle_store": STATE,
                                        "state_store": "s3"})  # fmt: skip
    services = Services.build(cfg)
    sm = boto3.client("secretsmanager", region_name="eu-west-1")
    if not sm.list_secrets()["SecretList"]:
        sm.create_secret(Name="/prd/pcp/rd-dpl-studydata-dev/vault-token", SecretString=TOKEN)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = DataopsVaultProvider(client, services.aws, address="https://vault.dataops.example",
                                    entity=lambda: "rd-dpl-studydata-dev", service_env="prd",
                                    token_secret="/{service_env}/pcp/{entity}/vault-token", token_region="eu-west-1",
                                    kv_path="secret/data/{entity_path}/base/{path}")  # fmt: skip
    return provider, services


def _ref(key: str | None = "api_key") -> ResolvedRef:
    return ResolvedRef("portal_client", "dataops_vault", "selfservice", key, None, "api_key")


def test_dataops_vault_reads_the_project_secret(aws: None) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"data": {"api_key": "k-123-synthetic"}}})

    provider, _ = _vault(handler)
    value = provider.get(_ref(), purpose="test")
    with value.reveal() as v:
        assert v == "k-123-synthetic"
    assert seen[0].url.path == "/v1/secret/data/rd/dpl/studydata/dev/base/selfservice"
    assert seen[0].headers["X-Vault-Token"] == TOKEN
    assert "k-123-synthetic" not in repr(value)
    assert "k-123-synthetic" not in jsonlog.redact("k-123-synthetic x")


def test_dataops_vault_failures_are_classified(aws: None) -> None:
    calls = {"n": 0}

    def rotated(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403 if calls["n"] == 1 else 200, json={"data": {"data": {"api_key": "v"}}})

    provider, _ = _vault(rotated)
    provider.get(_ref(), purpose="test")
    assert calls["n"] == 2  # one re-read of the token after a 403

    for status, err in ((404, ConfigError), (503, TransientError)):
        p, _ = _vault(lambda r, s=status: httpx.Response(s))
        with pytest.raises(err):
            p.get(_ref(), purpose="test")
    p, _ = _vault(lambda r: httpx.Response(200, json={"data": {"data": {"a": "1", "b": "2"}}}))
    with pytest.raises(ConfigError, match="several fields"):
        p.get(_ref(None), purpose="test")


def test_project_entity_follows_the_dataops_role_naming() -> None:
    assert project_entity(None, {"AWS_ROLE_ARN": "arn-rd-dpl-studydata-dev-role"}) == "rd-dpl-studydata-dev"
    assert project_entity("given-entity", {}) == "given-entity"
    with pytest.raises(ConfigError):
        project_entity(None, {"AWS_ROLE_ARN": "short-arn"})
