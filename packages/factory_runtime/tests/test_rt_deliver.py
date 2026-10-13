"""destination.deliver, the static puts and s3.copy as ops and as a task (ADR-0024, Q9, P3-3, P3-4).

The worked example in dev: x is the team's delivery bucket (moto), y the dev SFTP stub (asyncssh, seeded keys); the
SFTP key comes from the fake Vault. unit_static covers s3.put, sftp.put, https.put and s3.copy. Synthetic data only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from factory_core.errors import DataError, SecurityError
from factory_core.runkey import run_dir
from factory_runtime.artifacts import doc_name
from factory_runtime.config import Overrides, RuntimeConfig
from factory_runtime.state.dynamodb import DynamoRunState
from factory_spec.artifact_models import DestinationSet, ResolvedDestination
from factory_testing.certs import TestCA
from factory_testing.fake_vault import FakeVault
from factory_testing.https_sink import HttpsSink
from factory_testing.keys import CLIENT_KEY, DEV_SFTP_HOST, private_openssh, seeded_ed25519
from factory_testing.netmap import netmap
from factory_testing.sftp_server import SftpServer
from factory_testing.synthetic import put_package, study_package
from rt_helpers import (
    EXAMPLE_REPO,
    UNIT_REPO,
    LogCapture,
    OpHarness,
    copy_repo,
    make_config,
    publish_bundle,
    run_task,
    setup_stores,
)

from factory_runtime import delivery

REGION = "eu-west-1"
LANDING = "acme-clinops-landing-dev"
PREFIX = "incoming/ONC101/PKG0042/"
DELIVERY = "acme-clinops-delivery-dev"
VAULT_HOST = "vault.dev.acme.example"
NAME = "ONC101_PKG0042.zip"
X_URI = f"s3://{DELIVERY}/studies/ONC101/in/"
Y_URI = f"sftp://{DEV_SFTP_HOST}/inbound/ONC101/"
T1 = ["r_list_objects", "r_check_listing", "r_download", "r_zip", "r_manifest", "r_checksum", "r_package_name"]


def _s3() -> Any:
    return boto3.client("s3", region_name=REGION)


def _bucket(name: str) -> None:
    _s3().create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION})


def _message(files: list[Any]) -> dict[str, Any]:
    return {
        "request_id": "REQ-00000001",
        "study_id": "ONC101",
        "package_id": "PKG0042",
        "bucket": LANDING,
        "prefix": PREFIX,
        "file_count": len(files),
        "total_bytes": sum(len(f.data) for f in files),
    }


def _dset(x: str | None = X_URI, y: str | None = Y_URI, *, x_entry: str = "dev_delivery_s3") -> DestinationSet:
    d = {}
    if x:
        d["x"] = ResolvedDestination(slot="x", entry_id=x_entry, type=x.split(":")[0], uri=x)  # type: ignore[arg-type]
    if y:
        d["y"] = ResolvedDestination(slot="y", entry_id="dev_stub_sftp", type="sftp", uri=y)
    return DestinationSet(env="dev", destinations=d)


@pytest.fixture
def ca() -> TestCA:
    return TestCA()


@pytest.fixture
def stand_ins(tmp_path: Path, ca: TestCA) -> Iterator[tuple[SftpServer, FakeVault]]:
    vault = FakeVault(
        ca, VAULT_HOST, role="factory-clinops_data-study_pkg_delivery", server_id=VAULT_HOST, namespace="clinops"
    )
    vault.put("clinops/dev/partner-sftp", {"private_key": private_openssh(seeded_ed25519(CLIENT_KEY)).decode()})
    vault.start()
    with (
        SftpServer(tmp_path / "sftp-root") as sftp,
        netmap({(VAULT_HOST, 8200): ("127.0.0.1", vault.port), (DEV_SFTP_HOST, 22): ("127.0.0.1", sftp.port)}),
    ):
        yield sftp, vault
    vault.stop()


@pytest.fixture
def example(tmp_path: Path, workdir: Path, aws: None) -> tuple[Path, RuntimeConfig]:
    repo = copy_repo(EXAMPLE_REPO, tmp_path / "repo")
    cfg = make_config(
        workdir,
        "study_pkg_delivery",
        vault_address=f"https://{VAULT_HOST}:8200",
        vault_role="factory-clinops_data-study_pkg_delivery",
        vault_iam_server_id=VAULT_HOST,
    )
    setup_stores(cfg)
    for b in (LANDING, DELIVERY):
        _bucket(b)
    return repo / "pipelines" / "study_pkg_delivery", cfg


@pytest.fixture
def packaged(example: tuple[Path, RuntimeConfig], ca: TestCA) -> Callable[[], OpHarness]:
    """A harness whose run has the zip, manifest, checksum and name of t1/t2/t3 (portal step replaced by a set)."""
    pdir, cfg = example
    files = study_package(files=6)
    put_package(_s3(), LANDING, PREFIX, files)

    def make() -> OpHarness:
        h = OpHarness(pdir, cfg, trigger_body=_message(files), overrides=Overrides(extra_ca_pem=ca.cert_pem))
        for r in T1:
            h.run_rule(r)
        return h

    return make


def _staged_zip(h: OpHarness) -> bytes:
    pkg = h.store.get("package").value
    data: bytes = _s3().get_object(Bucket=pkg.bucket, Key=pkg.key)["Body"].read()
    return data


def test_deliver_x_s3_and_y_sftp(packaged: Callable[[], OpHarness], stand_ins: tuple[SftpServer, FakeVault]) -> None:
    sftp, _ = stand_ins
    h = packaged()
    h.put("destinations", _dset())
    x = h.run_rule("r_upload_x")[0]
    y = h.run_rule("r_upload_y")[0]
    data = _staged_zip(h)
    manifest = h.store.get("manifest").value
    assert (x.outcome, x.verification, x.target) == ("written", "read_back", X_URI + NAME)
    assert (y.outcome, y.verification, y.target) == ("written", "read_back", Y_URI + NAME)
    assert x.sha256 == y.sha256 == hashlib.sha256(data).hexdigest()
    s3 = _s3()
    at_x = {
        n: s3.get_object(Bucket=DELIVERY, Key=f"studies/ONC101/in/{n}")["Body"].read()
        for n in (NAME, f"{NAME}.manifest.json", f"{NAME}.md5", f"{NAME}.sha256")
    }
    folder = sftp.file("/inbound/ONC101")
    at_y = {p.name: p.read_bytes() for p in folder.iterdir()}
    assert at_x == at_y  # identical files in x and y
    assert at_x[NAME] == data
    assert at_x[f"{NAME}.manifest.json"] == manifest.canonical_json()
    # GNU coreutils format: `sha256sum -c ONC101_PKG0042.zip.sha256` accepts it
    assert at_x[f"{NAME}.sha256"] == f"{hashlib.sha256(data).hexdigest()}  {NAME}\n".encode()
    assert at_x[f"{NAME}.md5"] == f"{hashlib.md5(data, usedforsecurity=False).hexdigest()}  {NAME}\n".encode()
    head = s3.head_object(Bucket=DELIVERY, Key=f"studies/ONC101/in/{NAME}")
    assert head["SSEKMSKeyId"].endswith("alias/clinops-delivery-dev")
    events = [e for e in h.audit.events if e["type"] == "delivery"]
    assert [(e["slot"], e["destination_type"], e["verification"]) for e in events] == [
        ("x", "s3", "read_back"),
        ("y", "sftp", "read_back"),
    ]
    access = [e for e in h.audit.events if e["type"] == "secret_access"]
    assert [(e["secret"], e["purpose"], e["outcome"]) for e in access] == [("partner_sftp_key", "sftp delivery", "ok")]
    # a rerun of both rules finds the files there and keeps the first receipts (first answer wins)
    assert h.run_rule("r_upload_x")[0] == x
    assert h.run_rule("r_upload_y")[0] == y
    assert [p.name for p in folder.iterdir() if p.name.startswith(".")] == []  # no temp files left


def test_delivery_order_puts_sidecars_last(
    packaged: Callable[[], OpHarness], stand_ins: tuple[SftpServer, FakeVault], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = delivery.open_target

    class Recorder:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def put_package(self, name: str, src: Any, *, verify: bool) -> Any:
            calls.append(f"package {name}")
            return self.inner.put_package(name, src, verify=verify)

        def put_small(self, name: str, data: bytes, content_type: str) -> Any:
            calls.append(f"small {name}")
            return self.inner.put_small(name, data, content_type)

        def close(self) -> None:
            self.inner.close()

    monkeypatch.setattr(delivery, "open_target", lambda *a: Recorder(real(*a)))
    h = packaged()
    h.put("destinations", _dset())
    h.run_rule("r_upload_y")
    assert calls == [f"package {NAME}", f"small {NAME}.manifest.json", f"small {NAME}.md5", f"small {NAME}.sha256"]


@pytest.mark.parametrize(
    ("dset", "code"),
    [
        (_dset(x=f"s3://{DELIVERY}/studies/ONC999/in/"), "destination_not_allowed"),  # another study's folder
        (_dset(x=f"s3://{DELIVERY}/studies/ONC101/../ONC999/"), "destination_uri_invalid"),
        (_dset(x="s3://acme-attacker-bucket/studies/ONC101/in/"), "destination_not_allowed"),
        (_dset(x_entry="dev_stub_sftp"), "destination_not_allowed"),  # entry id swapped in the staged document
    ],
)
def test_staged_destination_set_is_resolved_again(
    packaged: Callable[[], OpHarness], dset: DestinationSet, code: str
) -> None:
    h = packaged()
    h.put("destinations", dset)
    with pytest.raises(SecurityError) as ei:
        h.run_rule("r_upload_x")
    assert ei.value.code == code
    assert _s3().list_objects_v2(Bucket=DELIVERY).get("KeyCount", 0) == 0  # nothing delivered


def test_tampered_staged_package_is_not_delivered(
    packaged: Callable[[], OpHarness], stand_ins: tuple[SftpServer, FakeVault]
) -> None:
    sftp, _ = stand_ins
    h = packaged()
    h.put("destinations", _dset())
    pkg = h.store.get("package").value
    data = _staged_zip(h)
    _s3().put_object(Bucket=pkg.bucket, Key=pkg.key, Body=data[:-1] + bytes([data[-1] ^ 1]))  # same size
    with pytest.raises(DataError) as ei:
        h.run_rule("r_upload_y")
    assert ei.value.code == "staged_package_changed"
    _s3().put_object(Bucket=pkg.bucket, Key=pkg.key, Body=b"PK shorter")  # a size change is caught while reading
    with pytest.raises(DataError) as ei:
        h.run_rule("r_upload_y")
    assert ei.value.code == "source_size_mismatch"
    assert list(sftp.file("/inbound/ONC101").iterdir()) == []


def test_x_delivered_y_transient_then_retry_completes_y(
    packaged: Callable[[], OpHarness],
    example: tuple[Path, RuntimeConfig],
    stand_ins: tuple[SftpServer, FakeVault],
    ca: TestCA,
) -> None:
    """Q9: x and y are both required; a retry of the task delivers the missing one and leaves x alone."""
    pdir, cfg = example
    sftp, _ = stand_ins
    h = packaged()  # t1/t2 of the same run (same run key): staged package, manifest, checksum
    h.staging.put_doc("trigger.json", h.trigger)  # type: ignore[arg-type]
    h.staging.put_doc(doc_name("destinations"), _dset())
    uri, sha = publish_bundle(cfg, pdir)
    overrides = Overrides(extra_ca_pem=ca.cert_pem)
    with netmap({(DEV_SFTP_HOST, 22): ("127.0.0.1", 1)}), LogCapture() as first:  # y unreachable
        assert run_task(cfg, uri, sha, "t3_upload", "REQ-00000001", overrides=overrides) == 75
    x_key = f"studies/ONC101/in/{NAME}"
    x_modified = _s3().head_object(Bucket=DELIVERY, Key=x_key)["LastModified"]
    state = DynamoRunState(boto3.client("dynamodb", region_name=REGION), cfg.run_state_table, cfg.pipeline_id)
    rdir = run_dir("REQ-00000001")
    assert state.get(rdir, "RULE#t3_upload#r_upload_x") is not None
    assert state.get(rdir, "RULE#t3_upload#r_upload_y") is None
    assert [e["code"] for e in first.events("task_failed")] == ["sftp_unavailable"]
    with LogCapture() as second:
        assert run_task(cfg, uri, sha, "t3_upload", "REQ-00000001", attempt=2, overrides=overrides) == 0
    skipped = [e["rule_id"] for e in second.events("rule_skipped_checkpoint")]
    assert "r_upload_x" in skipped
    assert "r_upload_y" not in skipped
    assert _s3().head_object(Bucket=DELIVERY, Key=x_key)["LastModified"] == x_modified  # x untouched
    assert sftp.file(f"/inbound/ONC101/{NAME}").read_bytes() == _staged_zip(h)
    receipt = json.loads(h.staging.get_bytes(doc_name("delivered_y")))
    assert receipt["outcome"] == "written"
    assert receipt["slot"] == "y"
    logs = first.text + second.text
    key_lines = private_openssh(seeded_ed25519(CLIENT_KEY)).decode().splitlines()
    assert not any(line in logs for line in key_lines if len(line) > 8)  # the SFTP key never reaches a log line


# ---------------------------------------------------------------- static puts and s3.copy (unit_static)

UPLOAD_HOST = "upload.dev.acme.example"
TOKEN = "synthetic-upload-token-55e1c0"
STATIC = ["r_list", "r_download", "r_zip", "r_manifest", "r_name", "r_store_s3", "r_store_sftp", "r_upload", "r_copy"]
OUTBOX = {"outbox/a.csv": b"id,value\n1,2\n" * 40, "outbox/sub/b.csv": b"id,value\n3,4\n" * 25}


@pytest.fixture
def unit_static(tmp_path: Path, workdir: Path, aws: None, ca: TestCA) -> Iterator[Callable[..., OpHarness]]:
    repo = copy_repo(UNIT_REPO, tmp_path / "repo", environments=["dev"])
    cfg = make_config(workdir, "unit_static")
    for b in (cfg.staging_bucket, cfg.audit_store, "unit-outbox-dev", "unit-store-dev", "unit-archive-dev"):
        _bucket(b)
    for k, v in OUTBOX.items():
        _s3().put_object(Bucket="unit-outbox-dev", Key=k, Body=v)
    sm = boto3.client("secretsmanager", region_name=REGION)
    key = private_openssh(seeded_ed25519(CLIENT_KEY)).decode()
    sm.create_secret(Name="factory/unit/dev/partner-sftp", SecretString=json.dumps({"private_key": key}))
    sm.create_secret(Name="factory/unit/dev/upload", SecretString=json.dumps({"token": TOKEN}))
    sink = HttpsSink(ca, UPLOAD_HOST, token=TOKEN)
    sink.start()
    with (
        SftpServer(tmp_path / "sftp-root") as sftp,
        netmap({(UPLOAD_HOST, 443): ("127.0.0.1", sink.port), (DEV_SFTP_HOST, 22): ("127.0.0.1", sftp.port)}),
    ):

        def make(batch: str = "B001", run_key: str = "REQ-00000001") -> OpHarness:
            h = OpHarness(
                repo / "pipelines" / "unit_static",
                cfg,
                trigger_body={"request_id": run_key, "batch_id": batch},
                overrides=Overrides(extra_ca_pem=ca.cert_pem),
                run_key=run_key,
            )
            h.sink = sink  # type: ignore[attr-defined]
            h.sftp = sftp  # type: ignore[attr-defined]
            return h

        yield make
    sink.stop()


def test_static_puts_and_copy(unit_static: Callable[..., OpHarness]) -> None:
    h = unit_static()
    out = {r: h.run_rule(r) for r in STATIC}
    data = _staged_zip(h)
    s3_r, sftp_r, https_r, copy_r = (out[r][0] for r in ("r_store_s3", "r_store_sftp", "r_upload", "r_copy"))
    assert (s3_r.target, s3_r.manifest) == ("s3://unit-store-dev/packages/B001/B001.zip", True)
    assert _s3().get_object(Bucket="unit-store-dev", Key="packages/B001/B001.zip")["Body"].read() == data
    manifest = _s3().get_object(Bucket="unit-store-dev", Key="packages/B001/B001.zip.manifest.json")["Body"].read()
    assert manifest == h.store.get("manifest").value.canonical_json()
    assert sftp_r.target == f"sftp://{DEV_SFTP_HOST}/inbound/B001/B001.zip"
    assert h.sftp.file("/inbound/B001/B001.zip").read_bytes() == data  # type: ignore[attr-defined]
    assert (https_r.verification, https_r.manifest) == ("digest", False)
    assert h.sink.files["/api/v1/batches/B001/B001.zip"] == data  # type: ignore[attr-defined]
    assert (copy_r.outcome, copy_r.verification, copy_r.size) == (
        "written",
        "size_only",
        sum(map(len, OUTBOX.values())),
    )
    assert [(f.path, f.outcome) for f in copy_r.files] == [("a.csv", "written"), ("sub/b.csv", "written")]
    for k, v in OUTBOX.items():
        got = _s3().get_object(Bucket="unit-archive-dev", Key="copies/B001/" + k.removeprefix("outbox/"))
        assert got["Body"].read() == v
        assert got["Metadata"]["factory-source-etag"] == hashlib.md5(v, usedforsecurity=False).hexdigest()
    # a retry keeps every first receipt and writes nothing again
    for r in ("r_store_s3", "r_store_sftp", "r_upload", "r_copy"):
        assert h.run_rule(r) == out[r]
    assert sum(1 for q in h.sink.requests if q.method == "PUT") == 1  # type: ignore[attr-defined]


def test_copy_refuses_to_replace_another_object(unit_static: Callable[..., OpHarness]) -> None:
    _s3().put_object(Bucket="unit-archive-dev", Key="copies/B002/a.csv", Body=b"someone else's file")
    h = unit_static("B002", "REQ-00000002")
    h.run_rule("r_list")
    with pytest.raises(DataError) as ei:
        h.run_rule("r_copy")
    assert ei.value.code == "destination_conflict"
    assert _s3().get_object(Bucket="unit-archive-dev", Key="copies/B002/a.csv")["Body"].read() == b"someone else's file"


@pytest.mark.parametrize("batch", ["..", "B001/../B999", "a b"])
def test_static_path_with_unsafe_value_is_security(unit_static: Callable[..., OpHarness], batch: str) -> None:
    h = unit_static(batch)
    for r in STATIC[:5]:
        try:
            h.run_rule(r)
        except SecurityError:  # the naming rule refuses it first for some values
            return
    with pytest.raises(SecurityError) as ei:
        h.run_rule("r_store_s3")
    assert ei.value.code == "destination_not_allowed"
