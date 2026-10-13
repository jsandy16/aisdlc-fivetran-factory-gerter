"""Pure logic shared by the runtime and the CLI: destination resolution, bundle, plugin import check, owner tests."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from factory_core.errors import ConfigError, SecurityError
from factory_spec.artifact_models import ChecksumRecord, ObjectEntry, ObjectListing, PortalSnapshot, facet
from factory_spec.bundle import MANIFEST, build_bundle, extract_bundle, verify_sha
from factory_spec.estimate import estimate_lambda_seconds, estimate_seconds
from factory_spec.imports import check_plugin_source
from factory_spec.load import load_spec
from factory_spec.owner_tests import run_owner_tests
from factory_spec.resolve import canonical_uri, resolve_destination, resolve_slots, target_segments
from factory_spec.spec import PipelineSpec
from spec_helpers import EXAMPLE_REPO, PIPELINE, edit

EXAMPLE = EXAMPLE_REPO / "pipelines" / PIPELINE
VARS = {"study_id": "ONC101", "package_id": "PKG0042"}


@pytest.fixture(scope="module")
def spec() -> PipelineSpec:
    s = load_spec(EXAMPLE).spec
    assert s is not None
    return s


# ---------------------------------------------------------------- destination resolution

GOOD = [
    ("dev", "x", "s3://acme-clinops-delivery-dev/studies/ONC101/in/", "dev_delivery_s3"),
    ("dev", "y", "s3://acme-clinops-delivery-dev/studies/ONC101/", "dev_delivery_s3"),
    ("dev", "y", "sftp://sftp-stub.dev.acme.example/inbound/ONC101/drop/", "dev_stub_sftp"),
    ("prod", "x", "https://upload.partner-cro.example.com/api/v2/studies/ONC101/packages", "cro_https_upload"),
    ("prod", "x", "sftp://sftp.partner-cro.example.com:22/inbound/acme/ONC101/", "partner_sftp"),
    ("prod", "y", "s3://acme-cro-archive-prod/studies/ONC101/packages/2026/", "cro_archive_s3"),
]

ATTACKS = [
    # wrong study, wrong host/bucket/port, wrong environment's entries
    ("dev", "x", "s3://acme-clinops-delivery-dev/studies/ONC999/in/"),
    ("dev", "x", "s3://acme-clinops-delivery-test/studies/ONC101/in/"),
    ("dev", "x", "s3://evil-bucket/studies/ONC101/in/"),
    ("dev", "x", "s3://acme-clinops-delivery-dev/other/ONC101/"),
    ("dev", "x", "s3://acme-clinops-delivery-dev/"),
    ("dev", "y", "sftp://sftp-stub.dev.acme.example:2222/inbound/ONC101/"),
    ("dev", "y", "sftp://evil.example.com/inbound/ONC101/"),
    ("dev", "y", "sftp://sftp-stub.dev.acme.example/outbound/ONC101/"),
    ("dev", "y", "sftp://sftp-stub.dev.acme.example/inbound/ONC999/"),
    ("prod", "x", "https://upload.partner-cro.example.com/api/v2/studies/ONC101/packages/extra"),
    ("prod", "x", "https://upload.partner-cro.example.com/api/v2/studies/ONC101"),
    ("prod", "x", "https://upload.partner-cro.example.com:8443/api/v2/studies/ONC101/packages"),
    ("prod", "x", "https://upload.partner-cro.example.com.evil.com/api/v2/studies/ONC101/packages"),
    ("prod", "y", "https://upload.partner-cro.example.com/api/v2/studies/ONC101/packages"),  # y allows s3/sftp only
    ("prod", "x", "s3://acme-clinops-delivery-dev/studies/ONC101/in/"),  # dev entry used in prod
    # encoding and traversal tricks
    ("dev", "x", "s3://acme-clinops-delivery-dev/studies/ONC101/../ONC999/"),
    ("dev", "x", "s3://acme-clinops-delivery-dev/studies/%2e%2e/ONC101/"),
    ("dev", "x", "s3://acme-clinops-delivery-dev/studies/ONC101%2F..%2F/"),
    ("dev", "x", "s3://acme-clinops-delivery-dev//studies/ONC101/"),
    ("dev", "x", "s3://acme-clinops-delivery-dev/studies/./ONC101/"),
    ("dev", "x", "s3://ACME-clinops-delivery-dev/studies/ONC101/"),
    ("dev", "x", "s3://acme-clinops-delivery-dev/studies/ONC101/?x=1"),
    ("dev", "x", "s3://acme-clinops-delivery-dev/studies/ONC101/#frag"),
    ("dev", "x", "s3://acme-clinops-delivery-dev/studies/ONC101\\x/"),
    ("dev", "x", "s3://acme-clinops-delivery-dev/studies/ONC101 /"),
    ("dev", "x", "s3://acme-clinops-delivery-dev/studies/ONC1०1/"),
    ("dev", "y", "sftp://user@sftp-stub.dev.acme.example/inbound/ONC101/"),
    ("dev", "y", "sftp://sftp-stub.dev.acme.example@evil.example.com/inbound/ONC101/"),
    ("dev", "y", "sftp://127.0.0.1/inbound/ONC101/"),
    ("dev", "y", "sftp://[::1]/inbound/ONC101/"),
    ("dev", "y", "sftp://localhost/inbound/ONC101/"),
    ("dev", "y", "sftp://sftp-stub.dev.acme.example./inbound/ONC101/"),
    ("dev", "y", "sftp://sftp-stub.dev.acme.example/inbound/ONC101;rm/"),
    ("prod", "x", "http://upload.partner-cro.example.com/api/v2/studies/ONC101/packages"),
    ("prod", "x", "ftp://upload.partner-cro.example.com/api/v2/studies/ONC101/packages"),
    ("prod", "x", "file:///etc/passwd"),
    ("prod", "x", "https://upload.partner-cro.example.com/api/v2/studies/ONC101/packages\x00"),
    ("prod", "x", "https://uplоad.partner-cro.example.com/api/v2/studies/ONC101/packages"),
    ("prod", "x", "https://xn--upload-partner.example.com/api/v2/studies/ONC101/packages"),
    ("prod", "x", ""),
    ("prod", "x", "https://upload.partner-cro.example.com/API/v2/studies/ONC101/packages"),
]


@pytest.mark.parametrize(("env", "slot", "uri", "entry"), GOOD)
def test_resolve_accepts_approved(spec: PipelineSpec, env: str, slot: str, uri: str, entry: str) -> None:
    r = resolve_destination(spec, env, slot, uri, VARS)
    assert r.entry.id == entry
    assert canonical_uri(r.uri).endswith("/")
    assert r.as_doc().entry_id == entry


@pytest.mark.parametrize(("env", "slot", "uri"), ATTACKS)
def test_resolve_rejects_attacks(spec: PipelineSpec, env: str, slot: str, uri: str) -> None:
    with pytest.raises(SecurityError) as ei:
        resolve_destination(spec, env, slot, uri, VARS)
    assert ei.value.code in ("destination_not_allowed", "destination_uri_invalid")


def test_resolve_needs_the_runs_own_values(spec: PipelineSpec) -> None:
    with pytest.raises(SecurityError):
        resolve_destination(spec, "dev", "x", "s3://acme-clinops-delivery-dev/studies/ONC101/", {})


def test_missing_required_slot_and_ambiguity(spec: PipelineSpec, pipeline_dir: Path) -> None:
    with pytest.raises(ConfigError) as ei:
        resolve_slots(spec, "dev", ["x", "y"], {"x": "s3://acme-clinops-delivery-dev/studies/ONC101/"}, VARS)
    assert ei.value.code == "destination_slot_missing"

    def dup(doc: object) -> None:
        import copy

        e = copy.deepcopy(doc["destinations"]["dev"][0])  # type: ignore[index]
        e["id"] = "dev_delivery_s3_again"
        doc["destinations"]["dev"].append(e)  # type: ignore[index]

    edit(pipeline_dir, "destinations.yaml", dup)
    s2 = load_spec(pipeline_dir).spec
    assert s2 is not None
    with pytest.raises(ConfigError) as ei:
        resolve_destination(s2, "dev", "x", "s3://acme-clinops-delivery-dev/studies/ONC101/", VARS)
    assert ei.value.code == "destination_ambiguous"


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../x.zip", "x\\y", "-x", "x y.zip", "é.zip"])
def test_target_segments_refuse_unsafe_names(name: str) -> None:
    with pytest.raises(SecurityError):
        target_segments(("studies", "ONC101"), name)


def test_target_segments() -> None:
    assert target_segments(("studies", "ONC101"), "ONC101_PKG0042.zip") == ("studies", "ONC101", "ONC101_PKG0042.zip")


# ---------------------------------------------------------------- owner examples


def test_owner_examples_of_the_worked_example_pass(spec: PipelineSpec) -> None:
    results = run_owner_tests(spec)
    assert {(r.rule_id, r.case): r.status for r in results} == {
        ("r_resolve_destinations", "approved_destinations"): "pass",
        ("r_resolve_destinations", "other_study_rejected"): "pass",
        ("r_check_listing", "complete_upload"): "pass",
        ("r_check_listing", "missing_file"): "pass",
        ("r_package_name", "example"): "pass",
    }


def test_owner_example_failure_is_reported(pipeline_dir: Path) -> None:
    edit(
        pipeline_dir,
        "rules.yaml",
        lambda doc: doc["rules"][-3]["test_cases"][0]["expect"].update(value="WRONG.zip"),  # r_package_name
    )
    s2 = load_spec(pipeline_dir).spec
    assert s2 is not None
    bad = [r for r in run_owner_tests(s2) if r.status == "fail"]
    assert [(r.rule_id, r.detail) for r in bad] == [("r_package_name", "got 'ONC101_PKG0042.zip'")]


# ---------------------------------------------------------------- bundle


def test_bundle_is_deterministic_and_extracts(tmp_path: Path) -> None:
    b1 = build_bundle(EXAMPLE)
    b2 = build_bundle(EXAMPLE)
    assert b1.data == b2.data
    names = zipfile.ZipFile(io.BytesIO(b1.data)).namelist()
    assert names == sorted(names)
    assert MANIFEST in names
    assert "spec.lock.json" in names
    assert all(i.compress_type == zipfile.ZIP_STORED for i in zipfile.ZipFile(io.BytesIO(b1.data)).infolist())
    verify_sha(b1.data, b1.sha256)
    manifest = extract_bundle(b1.data, tmp_path / "x")
    assert manifest["pipeline_id"] == PIPELINE
    assert (tmp_path / "x" / "spec" / "pipeline.yaml").read_bytes() == (EXAMPLE / "spec" / "pipeline.yaml").read_bytes()


def test_bundle_sha_is_stable_across_processes() -> None:
    code = (
        "import sys; from pathlib import Path; from factory_spec.bundle import build_bundle; "
        "print(build_bundle(Path(sys.argv[1])).sha256)"
    )
    shas = {
        subprocess.run(
            [sys.executable, "-c", code, str(EXAMPLE)],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for seed in ("0", "1")
    }
    assert shas == {build_bundle(EXAMPLE).sha256}


def test_bundle_refuses_stale_lock(pipeline_dir: Path) -> None:
    edit(pipeline_dir, "rules.yaml", lambda doc: doc["rules"][6]["implementation"]["params"].update(level=9))
    with pytest.raises(ConfigError) as ei:
        build_bundle(pipeline_dir)
    assert ei.value.code == "bundle_lock_stale"


def _rezip(data: bytes, mutate: dict[str, bytes | None]) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for info in src.infolist():
            if info.filename in mutate:
                new = mutate[info.filename]
                if new is not None:
                    zf.writestr(info, new)
            else:
                zf.writestr(info, src.read(info))
        for name, blob in mutate.items():
            if name not in src.namelist() and blob is not None:
                zf.writestr(zipfile.ZipInfo(name), blob)
    return out.getvalue()


@pytest.mark.parametrize(
    "mutate",
    [
        {"spec/rules.yaml": b"rules: []\n"},  # content no longer matches the manifest
        {"../evil.py": b"x"},
        {"rules/r_x/../../evil.py": b"x"},
        {"extra.txt": b"x"},
        {MANIFEST: None},
    ],
    ids=["tampered", "traversal", "nested_traversal", "unexpected", "no_manifest"],
)
def test_extract_refuses_tampering(tmp_path: Path, mutate: dict[str, bytes | None]) -> None:
    data = _rezip(build_bundle(EXAMPLE).data, mutate)
    with pytest.raises(SecurityError):
        extract_bundle(data, tmp_path / "x")
    assert not (tmp_path / "evil.py").exists()


def test_verify_sha_mismatch() -> None:
    with pytest.raises(SecurityError) as ei:
        verify_sha(b"abc", hashlib.sha256(b"abd").hexdigest())
    assert ei.value.code == "bundle_hash_mismatch"


# ---------------------------------------------------------------- plugin import check

ALLOWED = """from __future__ import annotations
import json
from collections import Counter
from factory_runtime.plugin_api import CustomRule, RuleContext

class Rule(CustomRule):
    def run(self, ctx, params, inputs):
        return json.dumps(sorted(Counter("abc")))
"""


def test_plugin_allowed_code_is_clean() -> None:
    assert check_plugin_source(ALLOWED) == []


@pytest.mark.parametrize(
    "code",
    [
        "import os",
        "import socket",
        "import subprocess",
        "import boto3",
        "import importlib",
        "from os import environ",
        "from factory_runtime import secrets",
        "from . import x",
        "x = __import__('os')",
        "eval('1')",
        "exec('x=1')",
        "open('/etc/passwd')",
        "getattr(object, 'x')",
        "x = ().__class__.__bases__",
        "x = '{0.__class__}'.format(1)",
        "from operator import attrgetter",
        "import pickle",
        "x = __builtins__",
        "def f(:",
    ],
)
def test_plugin_bypasses_are_found(code: str) -> None:
    assert check_plugin_source(code)


# ---------------------------------------------------------------- estimates and facets


def test_estimates(spec: PipelineSpec) -> None:
    tasks = {t.task_id: [spec.rule(r) for r in t.rules] for t in spec.pipeline.orchestration.tasks}
    assert estimate_lambda_seconds(spec, tasks["t2_checksum"]) == 116
    assert estimate_seconds(spec, tasks["t2_checksum"], "eks_pod", 10**10) < estimate_lambda_seconds(
        spec, tasks["t2_checksum"]
    )
    assert estimate_lambda_seconds(spec, tasks["t3_upload"]) > 800


def test_facets() -> None:
    listing = ObjectListing(
        bucket="b",
        prefix="p/",
        objects=[ObjectEntry(key="p/a", rel_path="a", size=3, etag="e")],
        count=1,
        total_bytes=3,
    )
    assert facet("object_listing", listing) == {"count": 1, "total_bytes": 3}
    assert facet("value", "x.zip") == "x.zip"
    snap = PortalSnapshot(endpoint="e", destinations={"x": "s3://b/p/", "y": None}, values={})
    assert facet("portal_snapshot", snap) == {"values": {}, "slots": ["x"]}
    ck = ChecksumRecord(size=3, digests={"md5": "m", "sha256": "s"}, compared_with_manifest=True)
    assert facet("checksum", ck) == {"size": 3, "md5": "m", "sha256": "s"}
    assert listing.doc_sha256() == ObjectListing.model_validate_json(listing.model_dump_json()).doc_sha256()
