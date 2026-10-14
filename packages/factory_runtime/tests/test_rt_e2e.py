"""Phase 3 acceptance: the worked example end to end in dev, plus the top security drills. Stand-ins only.

dispatcher → t1_collect → t2_checksum → t3_upload, with x = the team's S3 delivery bucket (moto) and y = the dev SFTP
stub (asyncssh, seeded keys), the portal and Vault as TLS stand-ins. Synthetic data and synthetic secrets only.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from factory_core.runkey import run_dir
from factory_runtime.config import Overrides
from factory_runtime.services import Services
from factory_testing.certs import TestCA
from factory_testing.fake_vault import FakeVault
from factory_testing.keys import CLIENT_KEY, DEV_SFTP_HOST, TEST_SFTP_HOST, private_openssh, seeded_ed25519
from factory_testing.mock_portal import MockPortal
from factory_testing.netmap import netmap
from factory_testing.sftp_server import SftpServer
from factory_testing.synthetic import put_package, study_package
from rt_helpers import EXAMPLE_REPO, LogCapture, copy_repo, make_config, publish_bundle, run_task, setup_stores

from factory_runtime import dispatch

REGION = "eu-west-1"
LANDING, DELIVERY = "acme-clinops-landing-dev", "acme-clinops-delivery-dev"
PREFIX = "incoming/ONC101/PKG0042/"
PORTAL, VAULT = "portal.dev.acme.example", "vault.dev.acme.example"
CLIENT = {"client_id": "synthetic-client-id", "client_secret": "synthetic-client-secret-e2e-0001"}
X = f"s3://{DELIVERY}/studies/ONC101/in/"
Y = f"sftp://{DEV_SFTP_HOST}/inbound/ONC101/"
NAME = "ONC101_PKG0042.zip"
TASKS = ("t1_collect", "t2_checksum", "t3_upload")


class Recorder:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}

    def start(self, dag_id: str, dag_run_id: str, conf: dict[str, Any]) -> str:
        if dag_run_id in self.runs:
            return "exists"
        self.runs[dag_run_id] = conf
        return "created"


@pytest.fixture
def world(tmp_path: Path, workdir: Path, aws: None) -> Iterator[dict[str, Any]]:
    ca = TestCA()
    repo = copy_repo(EXAMPLE_REPO, tmp_path / "repo")
    pdir = repo / "pipelines" / "study_pkg_delivery"
    cfg = make_config(workdir, "study_pkg_delivery", vault_address=f"https://{VAULT}:8200",
                      vault_role="factory-clinops_data-study_pkg_delivery", vault_iam_server_id=VAULT)  # fmt: skip
    setup_stores(cfg)
    s3 = boto3.client("s3", region_name=REGION)
    for b in (LANDING, DELIVERY):
        s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
    files = study_package(files=8)
    put_package(s3, LANDING, PREFIX, files)
    boto3.client("secretsmanager", region_name=REGION).create_secret(
        Name="factory/clinops/dev/portal-client", SecretString=json.dumps(CLIENT)
    )
    key = private_openssh(seeded_ed25519(CLIENT_KEY)).decode()
    vault = FakeVault(ca, VAULT, role="factory-clinops_data-study_pkg_delivery", server_id=VAULT, namespace="clinops")
    vault.put("clinops/dev/partner-sftp", {"private_key": key})
    portal = MockPortal(ca, PORTAL, client_id=CLIENT["client_id"], client_secret=CLIENT["client_secret"])
    portal.set_targets("ONC101", X, Y)
    uri, sha = publish_bundle(cfg, pdir)
    message = {"request_id": "REQ-00000001", "study_id": "ONC101", "package_id": "PKG0042", "bucket": LANDING,
               "prefix": PREFIX, "file_count": len(files), "total_bytes": sum(len(f.data) for f in files)}  # fmt: skip
    with vault, portal:
        yield {"ca": ca, "cfg": cfg, "uri": uri, "sha": sha, "portal": portal, "vault": vault, "message": message,
               "tmp": tmp_path,
               "secrets": [CLIENT["client_secret"], *[ln for ln in key.splitlines() if len(ln) > 8]]}  # fmt: skip


def _dispatch(w: dict[str, Any]) -> str:
    cfg = w["cfg"].model_copy(
        update={"bundle_uri": w["uri"], "bundle_sha256": w["sha"], "dag_id": "study_pkg_delivery"}
    )
    trigger = Recorder()
    d = dispatch.build(Services.build(cfg), trigger)
    response, _ = d.handle_batch({"Records": [{"messageId": "m-1", "body": json.dumps(w["message"])}]})
    assert response == {"batchItemFailures": []}
    (conf,) = trigger.runs.values()
    assert conf["bundle_sha256"] == w["sha"]
    return str(conf["run_key"])


def _run(w: dict[str, Any], task: str, run_key: str, sftp_port: int, **kw: Any) -> int:
    hosts = {(PORTAL, 443): ("127.0.0.1", w["portal"].port), (DEV_SFTP_HOST, 22): ("127.0.0.1", sftp_port)}
    with netmap(hosts):
        return run_task(w["cfg"], kw.pop("uri", w["uri"]), kw.pop("sha", w["sha"]), task, run_key,
                        overrides=Overrides(extra_ca_pem=w["ca"].cert_pem), **kw)  # fmt: skip


def _vault(w: dict[str, Any]) -> Any:
    return netmap({(VAULT, 8200): ("127.0.0.1", w["vault"].port)})


def test_worked_example_end_to_end(world: dict[str, Any]) -> None:
    w = world
    s3 = boto3.client("s3", region_name=REGION)
    with SftpServer(w["tmp"] / "sftp") as sftp, LogCapture() as logs, _vault(w):
        run_key = _dispatch(w)
        for task in TASKS:
            assert _run(w, task, run_key, sftp.port) == 0, task
        for task in TASKS:  # a rerun of every task does nothing
            assert _run(w, task, run_key, sftp.port, attempt=2) == 0
        at_y = {p.name: p.read_bytes() for p in sftp.file("/inbound/ONC101").iterdir()}
    at_x = {n: s3.get_object(Bucket=DELIVERY, Key=f"studies/ONC101/in/{n}")["Body"].read() for n in at_y}
    assert sorted(at_y) == [NAME, f"{NAME}.manifest.json", f"{NAME}.md5", f"{NAME}.sha256"]
    assert at_x == at_y  # identical package, manifest and checksum files at x and y
    manifest = json.loads(at_x[f"{NAME}.manifest.json"])
    assert manifest["file_count"] == w["message"]["file_count"]
    assert [e["status"] for e in logs.events("run_exit")] == ["succeeded"] * 3 + ["already_done"] * 3
    for value in w["secrets"]:  # canary: no secret value or key line in any log line
        assert value not in logs.text
    assert run_dir("REQ-00000001") in s3.list_objects_v2(Bucket=w["cfg"].audit_store)["Contents"][0]["Key"]


@pytest.mark.parametrize(
    "uri",
    [
        f"s3://{DELIVERY}/studies/ONC999/in/",  # another study's folder
        "s3://acme-attacker-bucket/studies/ONC101/in/",  # an unapproved bucket
        f"s3://{DELIVERY}/studies/ONC101/%2e%2e/ONC999/",  # encoded traversal
        "sftp://198.51.100.7/inbound/ONC101/",  # an IP address
        "sftp://user@sftp-stub.dev.acme.example/inbound/ONC101/",  # userinfo
    ],
)
def test_drill_portal_attack_uri_stops_the_run_with_77(world: dict[str, Any], uri: str) -> None:
    w = world
    w["portal"].set_targets("ONC101", uri, Y)
    with SftpServer(w["tmp"] / "sftp") as sftp, _vault(w):
        run_key = _dispatch(w)
        assert _run(w, "t1_collect", run_key, sftp.port) == 77
    assert boto3.client("s3", region_name=REGION).list_objects_v2(Bucket=DELIVERY).get("KeyCount", 0) == 0


def test_drill_wrong_sftp_host_key_is_77(world: dict[str, Any]) -> None:
    w = world
    with SftpServer(w["tmp"] / "sftp", host_key=TEST_SFTP_HOST) as sftp, _vault(w):  # not the pinned dev key
        run_key = _dispatch(w)
        assert [_run(w, t, run_key, sftp.port) for t in TASKS] == [0, 0, 77]
        assert not sftp.file("/inbound/ONC101").exists()


def test_drill_bundle_tampering_is_77(world: dict[str, Any]) -> None:
    w = world
    s3 = boto3.client("s3", region_name=REGION)
    bucket, key = w["uri"][5:].split("/", 1)
    data = bytearray(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    data[100] ^= 1
    s3.put_object(Bucket=bucket, Key=key, Body=bytes(data))  # the stored bundle no longer has its hash
    with SftpServer(w["tmp"] / "sftp") as sftp, _vault(w):
        assert _run(w, "t1_collect", "REQ-00000001", sftp.port) == 77


def test_s3_event_starter_end_to_end(tmp_path: Path, workdir: Path, aws: None) -> None:
    """s3_to_s3_event (filled): an S3 event names one object; it is copied to the archive, write-once."""
    from rt_helpers import UNIT_REPO

    repo = copy_repo(UNIT_REPO, tmp_path / "repo", environments=["dev"])
    pdir = repo / "pipelines" / "unit_s3_event"
    cfg = make_config(workdir, "unit_s3_event")
    setup_stores(cfg)
    s3 = boto3.client("s3", region_name=REGION)
    for b in ("unit-source-dev", "unit-archive-dev"):
        s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.put_bucket_versioning(Bucket="unit-source-dev", VersioningConfiguration={"Status": "Enabled"})
    put = s3.put_object(Bucket="unit-source-dev", Key="incoming/batch 7/a.csv", Body=b"id\n1\n")
    s3.put_object(Bucket="unit-source-dev", Key="incoming/batch 7/a.csv.bak", Body=b"not this one")
    uri, sha = publish_bundle(cfg, pdir)
    event = {"Records": [{"eventName": "ObjectCreated:Put", "s3": {"bucket": {"name": "unit-source-dev"}, "object": {
        "key": "incoming/batch+7/a.csv", "size": 5, "sequencer": "0A1", "versionId": put["VersionId"]}}}]}  # fmt: skip
    trigger = Recorder()
    dcfg = cfg.model_copy(update={"bundle_uri": uri, "bundle_sha256": sha, "dag_id": "unit_s3_event"})
    d = dispatch.build(Services.build(dcfg), trigger)
    assert d.handle_batch({"Records": [{"messageId": "m-1", "body": json.dumps(event)}]})[0] == {
        "batchItemFailures": []
    }
    (conf,) = trigger.runs.values()
    assert run_task(cfg, uri, sha, "t1_copy", conf["run_key"]) == 0
    archived = s3.list_objects_v2(Bucket="unit-archive-dev")["Contents"]
    assert [o["Key"] for o in archived] == ["copies/dev/a.csv"]
    assert s3.get_object(Bucket="unit-archive-dev", Key="copies/dev/a.csv")["Body"].read() == b"id\n1\n"


def test_generated_dag_commands_deliver_end_to_end(world: dict[str, Any]) -> None:
    """Phase 5 M5: the task commands of the generated DAG (not hand-written argv) run the worked example."""
    from factory.gen.dag import generate_dag
    from factory.gen.names import Deploy
    from factory_airflow.argv import runtime_argv
    from factory_runtime.run import main
    from factory_spec.load import load_spec

    w = world
    cfg = w["cfg"]
    spec = load_spec(w["tmp"] / "repo" / "pipelines" / "study_pkg_delivery").spec
    assert spec is not None
    deploy = Deploy(env="dev", team=cfg.team, region=REGION, bundle_uri=w["uri"], bundle_sha256=w["sha"],
                    image="factory-runtime@" + cfg.image_digest, staging_bucket=cfg.staging_bucket,
                    run_state_table=cfg.run_state_table, bundle_store=cfg.bundle_store,
                    audit_store=cfg.audit_store)  # fmt: skip
    from factory.local.runner import dag_tasks

    tasks = [(t, rt) for t, rt, _ in dag_tasks(generate_dag(spec, deploy, "f" * 64))]
    assert [t for t, _ in tasks] == list(TASKS)
    with SftpServer(w["tmp"] / "sftp") as sftp, _vault(w):
        run_key = _dispatch(w)
        dag_run_id = run_dir(run_key)
        hosts = {(PORTAL, 443): ("127.0.0.1", w["portal"].port), (DEV_SFTP_HOST, 22): ("127.0.0.1", sftp.port)}
        for _task, rt in tasks:
            rendered = {k: (v.replace("{{ (dag_run.conf or {}).get('run_key') or run_id }}", run_key)
                             .replace("{{ ti.try_number }}", "1").replace("{{ run_id }}", dag_run_id)
                            if isinstance(v, str) else v) for k, v in rt.items()}  # fmt: skip
            assert "{{" not in json.dumps(rendered)
            with netmap(hosts):
                code = main(runtime_argv(rendered), config=cfg, overrides=Overrides(extra_ca_pem=w["ca"].cert_pem),
                            environ={})  # fmt: skip
            assert code == 0, _task
        delivered = sorted(p.name for p in sftp.file("/inbound/ONC101").iterdir())
    assert delivered == [NAME, f"{NAME}.manifest.json", f"{NAME}.md5", f"{NAME}.sha256"]
