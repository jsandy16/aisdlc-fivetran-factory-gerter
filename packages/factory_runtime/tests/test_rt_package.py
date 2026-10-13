"""Streaming package path (ADR-0013): S3 transport, deterministic zip, s3.list/download, package, manifest, checksum."""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import boto3
import pytest
from factory_core.errors import DataError, SecurityError
from factory_runtime.config import Overrides
from factory_runtime.ops import CheckFailed
from factory_runtime.package.zipstream import DeterministicZip, HashingSink, ZipOptions, is_compressed, zip_files
from factory_runtime.transports.s3 import AlreadyExists, MultipartWriter, part_size_for
from factory_testing.synthetic import PNG_HEADER, put_package, study_package
from rt_helpers import EXAMPLE_REPO, OpHarness, copy_repo, make_config

LANDING = "acme-clinops-landing-dev"
PREFIX = "incoming/ONC101/PKG0042/"


class Unseekable(io.RawIOBase):
    def __init__(self) -> None:
        super().__init__()
        self.buf = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, b: Any) -> int:
        self.buf += bytes(b)
        return len(b)


def _zip(items: list[tuple[str, bytes]], **opts: Any) -> bytes:
    sink = Unseekable()
    zip_files([(p, len(d), io.BytesIO(d)) for p, d in items], sink, ZipOptions(**opts))
    return bytes(sink.buf)


# ---------------------------------------------------------------- zip writer

ITEMS = [("a.csv", b"x,y\n1,2\n" * 50), ("img/p.png", PNG_HEADER + b"\x00" * 500), ("z/deep/c.txt", b"hello")]


def test_zip_is_deterministic_and_readable() -> None:
    a = _zip(ITEMS)
    assert a == _zip(ITEMS)
    zf = zipfile.ZipFile(io.BytesIO(a))
    assert zf.namelist() == ["a.csv", "img/p.png", "z/deep/c.txt"]
    assert [i.compress_type for i in zf.infolist()] == [zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED]
    assert all(i.date_time == (1980, 1, 1, 0, 0, 0) and i.flag_bits & 0x08 for i in zf.infolist())
    assert {n: zf.read(n) for n in zf.namelist()} == dict(ITEMS)
    assert zf.testzip() is None


def test_store_only_zip_has_golden_bytes() -> None:
    """Same bytes on every Python version and platform (no zlib involved)."""
    data = _zip(ITEMS, compression="store")
    assert hashlib.sha256(data).hexdigest() == "842ad0745e334355912962473ca718351279201df1c580e59453499251afaa1b"


def test_zip64_modes() -> None:
    always = zipfile.ZipFile(io.BytesIO(_zip(ITEMS, zip64="always")))
    assert all(i.extract_version >= 45 for i in always.infolist())
    auto = zipfile.ZipFile(io.BytesIO(_zip(ITEMS)))
    assert all(i.extract_version < 45 for i in auto.infolist())


def test_zip_refuses_wrong_size_and_order() -> None:
    sink = Unseekable()
    z = DeterministicZip(sink, ZipOptions())
    with pytest.raises(DataError) as ei:
        z.add("a.csv", 999, io.BytesIO(b"short"))
    assert ei.value.code == "source_size_mismatch"
    z.abandon()
    z2 = DeterministicZip(Unseekable(), ZipOptions())
    z2.add("b", 1, io.BytesIO(b"1"))
    with pytest.raises(DataError):
        z2.add("a", 1, io.BytesIO(b"1"))
    z2.abandon()


def test_compressed_detection() -> None:
    assert is_compressed("x.PNG", b"")
    assert is_compressed("x.bin", b"\x1f\x8b\x08\x00")
    assert is_compressed("x.bin", b"\x00\x00\x00\x18ftypmp42")
    assert not is_compressed("x.csv", b"study,subject")


def test_hashing_sink() -> None:
    target = Unseekable()
    s = HashingSink(target)
    s.write(b"abc")
    assert s.sha256.hexdigest() == hashlib.sha256(b"abc").hexdigest()
    assert s.size == 3
    assert bytes(target.buf) == b"abc"


# ---------------------------------------------------------------- S3 transport


def test_part_size() -> None:
    assert part_size_for(10_000_000_000) == 64 * 1024 * 1024
    assert part_size_for(2_000_000_000_000) >= 2_000_000_000_000 // 10_000


def test_multipart_writer_is_write_once_and_cleans_up(aws: None) -> None:
    s3 = boto3.client("s3", region_name="eu-west-1")
    s3.create_bucket(Bucket="stage-bucket", CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    data = bytes(range(256)) * (12 * 4096)  # 12 MiB -> 3 parts of 5 MiB
    w = MultipartWriter(s3, "stage-bucket", "p/k.zip", part_size=5 * 1024 * 1024, concurrency=2)
    w.write(data)
    w.complete()
    assert s3.get_object(Bucket="stage-bucket", Key="p/k.zip")["Body"].read() == data
    w2 = MultipartWriter(s3, "stage-bucket", "p/k.zip", part_size=5 * 1024 * 1024)
    w2.write(data)
    with pytest.raises(AlreadyExists):
        w2.complete()
    assert s3.list_multipart_uploads(Bucket="stage-bucket").get("Uploads", []) == []
    small = MultipartWriter(s3, "stage-bucket", "p/small.zip")
    small.write(b"tiny")
    small.complete()
    again = MultipartWriter(s3, "stage-bucket", "p/small.zip")
    again.write(b"other")
    with pytest.raises(AlreadyExists):
        again.complete()


# ---------------------------------------------------------------- the t1/t2 rules of the worked example


def _message(files: list[Any], **kw: Any) -> dict[str, Any]:
    msg = {
        "request_id": "REQ-00000001",
        "study_id": "ONC101",
        "package_id": "PKG0042",
        "bucket": LANDING,
        "prefix": PREFIX,
        "file_count": len(files),
        "total_bytes": sum(len(f.data) for f in files),
    }
    msg.update(kw)
    return msg


@pytest.fixture
def setup(tmp_path: Path, workdir: Path, aws: None) -> Callable[..., OpHarness]:
    repo = copy_repo(EXAMPLE_REPO, tmp_path / "repo")
    pdir = repo / "pipelines" / "study_pkg_delivery"
    cfg = make_config(workdir, "study_pkg_delivery")
    s3 = boto3.client("s3", region_name="eu-west-1")
    for b in (cfg.staging_bucket, cfg.audit_store, LANDING, "acme-clinops-landing-test"):
        s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})

    def make(message: dict[str, Any], run_key: str = "REQ-00000001") -> OpHarness:
        return OpHarness(pdir, cfg, trigger_body=message, run_key=run_key, overrides=Overrides())

    return make


T1 = ["r_list_objects", "r_check_listing", "r_download", "r_zip", "r_manifest"]


def _staged_zip(h: OpHarness) -> bytes:
    pkg = h.store.get("package").value
    data: bytes = boto3.client("s3", region_name="eu-west-1").get_object(Bucket=pkg.bucket, Key=pkg.key)["Body"].read()
    return data


def test_collect_package_manifest_checksum(setup: Callable[..., OpHarness]) -> None:
    files = study_package()
    s3 = boto3.client("s3", region_name="eu-west-1")
    put_package(s3, LANDING, PREFIX, files)
    s3.put_object(Bucket=LANDING, Key=PREFIX + "visits/", Body=b"")  # folder marker: skipped
    s3.put_object(Bucket=LANDING, Key=PREFIX + "docs/.DS_Store", Body=b"junk")  # excluded by the rule
    h = setup(_message(files))
    for r in T1:
        h.run_rule(r)
    data = _staged_zip(h)
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.namelist() == [f.rel_path for f in files]  # folder structure below the message's folder
    assert all(zf.read(f.rel_path) == f.data for f in files)
    manifest = h.store.get("manifest").value
    assert {m.path: m.sha256 for m in manifest.files} == {f.rel_path: f.sha256 for f in files}
    assert manifest.package.sha256 == hashlib.sha256(data).hexdigest()
    assert manifest.package.md5 == hashlib.md5(data, usedforsecurity=False).hexdigest()
    assert "REQ-00000001" not in manifest.model_dump_json()
    ck = h.run_rule("r_checksum")[0]
    assert ck.digests["sha256"] == manifest.package.sha256
    assert ck.compared_with_manifest
    # same files, another run: byte-identical package
    h2 = setup(_message(files, request_id="REQ-00000002"), run_key="REQ-00000002")
    for r in T1:
        h2.run_rule(r)
    assert _staged_zip(h2) == data
    # a retry of the first run (new file arrived meanwhile) adopts the first listing and finds the same package
    s3.put_object(Bucket=LANDING, Key=PREFIX + "late.csv", Body=b"late")
    h3 = setup(_message(files))
    for r in T1:
        h3.run_rule(r)
    assert h3.store.get("listing").value.count == len(files)
    assert _staged_zip(h3) == data


def test_tampered_staged_package_fails_checksum(setup: Callable[..., OpHarness]) -> None:
    files = study_package(files=4)
    put_package(boto3.client("s3", region_name="eu-west-1"), LANDING, PREFIX, files)
    h = setup(_message(files))
    for r in T1:
        h.run_rule(r)
    pkg = h.store.get("package").value
    boto3.client("s3", region_name="eu-west-1").put_object(Bucket=pkg.bucket, Key=pkg.key, Body=b"PK tampered")
    with pytest.raises(DataError) as ei:
        h.run_rule("r_checksum")
    assert ei.value.code == "checksum_mismatch"


def test_listing_must_match_the_message(setup: Callable[..., OpHarness]) -> None:
    files = study_package(files=3)
    put_package(boto3.client("s3", region_name="eu-west-1"), LANDING, PREFIX, files)
    h = setup(_message(files, file_count=99))
    h.run_rule("r_list_objects")
    with pytest.raises(CheckFailed):
        h.run_rule("r_check_listing")


def test_source_changed_after_listing(setup: Callable[..., OpHarness]) -> None:
    files = study_package(files=3)
    s3 = boto3.client("s3", region_name="eu-west-1")
    put_package(s3, LANDING, PREFIX, files)
    h = setup(_message(files))
    h.run_rule("r_list_objects")
    h.run_rule("r_download")
    s3.put_object(Bucket=LANDING, Key=PREFIX + files[0].rel_path, Body=b"replaced after listing")
    with pytest.raises(DataError) as ei:
        h.run_rule("r_zip")
    assert ei.value.code in ("source_changed", "source_size_mismatch")


@pytest.mark.parametrize(
    ("change", "error", "code"),
    [
        ({"bucket": "acme-clinops-landing-test"}, SecurityError, "source_bucket_not_allowed"),
        ({"prefix": "outgoing/ONC101/"}, SecurityError, "source_prefix_not_allowed"),
        ({"prefix": "incoming/ONC101/PKG0042"}, DataError, "source_prefix_invalid"),
        ({"prefix": "incoming/../secret/"}, DataError, "source_prefix_invalid"),
    ],
)
def test_message_cannot_redirect_the_source(
    setup: Callable[..., OpHarness], change: dict[str, str], error: type[Exception], code: str
) -> None:
    files = study_package(files=2)
    with pytest.raises(error) as ei:
        setup(_message(files, **change)).run_rule("r_list_objects")
    assert ei.value.code == code  # type: ignore[attr-defined]


def test_unsafe_object_names_stop_the_run(setup: Callable[..., OpHarness]) -> None:
    s3 = boto3.client("s3", region_name="eu-west-1")
    s3.put_object(Bucket=LANDING, Key=PREFIX + "a/../b.csv", Body=b"x")
    with pytest.raises(DataError) as ei:
        setup(_message([])).run_rule("r_list_objects")
    assert ei.value.code == "object_key_unsafe"
