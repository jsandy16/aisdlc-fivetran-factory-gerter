"""Change request M3: `factory run` runs the worked example on this machine.

The framework's stores are on a local endpoint; a second local endpoint stands in for the dev account that holds the
real source and destination buckets; the portal and the partner SFTP server are TLS stand-ins; every secret comes
from the developer's local file (no Vault, no Secrets Manager). Synthetic data and synthetic secrets only.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import boto3
import pytest
from factory.local.runner import LocalRunOptions, run_local
from factory_spec.repo import load_repo
from factory_testing.certs import TestCA
from factory_testing.keys import CLIENT_KEY, DEV_SFTP_HOST, private_openssh, seeded_ed25519
from factory_testing.mock_portal import MockPortal
from factory_testing.moto_server import moto_subprocess
from factory_testing.netmap import netmap
from factory_testing.sftp_server import SftpServer
from factory_testing.synthetic import put_package, study_package

from factory_core import yamlio

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "team-pipelines"
SETTINGS = ROOT / "platform" / "platform-settings"
PID, REGION, STUDY = "study_pkg_delivery", "eu-west-1", "ONC101"
LANDING, DELIVERY = "acme-clinops-landing-dev", "acme-clinops-delivery-dev"
PORTAL = "portal.dev.acme.example"
CLIENT_SECRET = "synthetic-client-secret-localrun-0001"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    shutil.copytree(EXAMPLE, r, ignore=shutil.ignore_patterns("build", "__pycache__"))
    (r / "factory.toml").write_text(
        'api_version = "factory/v1"\nteam = "clinops_data"\nenvironments = ["dev", "test", "ppt", "prod"]\n'
        f'platform_settings = "{SETTINGS.as_posix()}"\n'
    )
    return r


def _secrets(tmp_path: Path) -> Path:
    key = private_openssh(seeded_ed25519(CLIENT_KEY)).decode()
    doc = {"clinops/dev/portal-client": {"client_id": "synthetic-client-id", "client_secret": CLIENT_SECRET},
           "clinops/dev/partner-sftp": {"private_key": key},
           "clinops/dev/cro-upload": {"token": "synthetic-token-localrun"}}  # fmt: skip
    f = tmp_path / "secrets" / "dev.yaml"
    f.parent.mkdir()
    f.write_text(yamlio.dump_text(doc))
    os.chmod(f, 0o600)
    return f


def test_local_run_of_the_worked_example(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("AWS_PROFILE", "AWS_ENDPOINT_URL", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    ca = TestCA()
    with moto_subprocess() as dev_account, MockPortal(ca, PORTAL, client_id="synthetic-client-id",
                                                  client_secret=CLIENT_SECRET) as portal, \
            SftpServer(tmp_path / "sftp") as sftp:  # fmt: skip
        portal.set_targets(STUDY, f"s3://{DELIVERY}/studies/{STUDY}/in/", f"sftp://{DEV_SFTP_HOST}/inbound/{STUDY}/")
        s3 = boto3.client("s3", region_name=REGION, endpoint_url=dev_account)
        for b in (LANDING, DELIVERY):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
        files = study_package(STUDY, "PKG0101", files=5)
        put_package(s3, LANDING, f"incoming/{STUDY}/PKG0101/", files)
        message = {"request_id": "REQ-LOCAL-0001", "study_id": STUDY, "package_id": "PKG0101", "bucket": LANDING,
                   "prefix": f"incoming/{STUDY}/PKG0101/", "file_count": len(files),
                   "total_bytes": sum(len(f.data) for f in files)}  # fmt: skip
        hosts = {(PORTAL, 443): ("127.0.0.1", portal.port), (DEV_SFTP_HOST, 22): ("127.0.0.1", sftp.port)}
        opts = LocalRunOptions(message=message, secrets_file=_secrets(tmp_path), workdir=tmp_path / "work",
                               extra_ca_pem=ca.cert_pem, remote_endpoint=dev_account,
                               hosts=lambda: netmap(hosts))  # fmt: skip
        report = run_local(load_repo(repo), repo / "pipelines" / PID, opts)
        log = report.pop("_log")
        name = f"{STUDY}_PKG0101.zip"
        at_x = sorted(o["Key"].rsplit("/", 1)[1] for o in s3.list_objects_v2(Bucket=DELIVERY)["Contents"])
        at_y = sorted(p.name for p in sftp.file(f"/inbound/{STUDY}").iterdir())
        dev_buckets = {b["Name"] for b in s3.list_buckets()["Buckets"]}
    assert report["ok"], report
    expected = [(t, "ran locally", 0) for t in ("t1_collect", "t2_checksum", "t3_upload")]
    assert [(t["task"], t["status"], t["exit_code"]) for t in report["tasks"]] == expected
    assert "portal.dev.acme.example:443" in report["tasks"][0]["remote_endpoints"]
    assert at_x == at_y == [name, f"{name}.manifest.json", f"{name}.md5", f"{name}.sha256"]
    assert dev_buckets == {LANDING, DELIVERY}  # staging, bundles and audit stayed on this machine
    assert CLIENT_SECRET not in log
    metrics = [json.loads(x) for x in log.splitlines() if '"SecretAccess"' in x]
    assert metrics  # every secret was read, and from the local file (no Vault or Secrets Manager exists here)
    assert {m["provider"] for m in metrics} == {"local_file"}


def test_local_runs_are_refused_outside_dev(repo: Path) -> None:
    from factory_core.envelope import CommandError

    for env in ("prod", "test"):  # prod: never; test: the platform does not allow local secrets there
        with pytest.raises(CommandError):
            run_local(load_repo(repo), repo / "pipelines" / PID, LocalRunOptions(env=env))


def test_skipped_tasks_are_reported(repo: Path, tmp_path: Path) -> None:
    opts = LocalRunOptions(message={"request_id": "REQ-1"}, skip=["t1_collect", "t2_checksum", "t3_upload"],
                           secrets_file=tmp_path / "none.yaml", data="local", workdir=tmp_path / "w")  # fmt: skip
    with pytest.raises(Exception, match="dispatcher refused"):  # the message lacks required fields
        run_local(load_repo(repo), repo / "pipelines" / PID, opts)
    good = {"request_id": "REQ-00000002", "study_id": STUDY, "package_id": "PKG0102", "bucket": LANDING,
            "prefix": "incoming/ONC101/PKG0102/", "file_count": 1, "total_bytes": 1}  # fmt: skip
    opts.message = good
    report = run_local(load_repo(repo), repo / "pipelines" / PID, opts)
    assert [t["status"] for t in report["tasks"]] == ["skipped"] * 3
    assert json.dumps(report["tasks"]).count("skipped on request") == 3
