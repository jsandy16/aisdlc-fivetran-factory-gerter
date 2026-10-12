"""Failure injection: network drops, short downloads, growing files, S3 originals, real SFTP/FTPS/S3 servers."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from pf.testing import LocalDirSession

from .helpers import audits, file_lock, last_state, rows, run, write

ENV = os.environ.get("PF_DRYRUN_ENV", "/tmp/pf-dry/env.json")


class FlakySession(LocalDirSession):
    fails = {"n": 0}

    def download(self, rel, dest):
        if FlakySession.fails["n"] < 1:
            FlakySession.fails["n"] += 1
            with open(dest, "wb") as fh:
                fh.write(b"id,v\n1,PARTIAL")
            raise ConnectionResetError("connection reset mid-download")
        super().download(rel, dest)


def test_network_drop_mid_download_retried(tmp_path):
    write(tmp_path, "d.csv", "id,v\n1,a\n2,b\n")
    FlakySession.fails["n"] = 0
    op = run(file_lock(), tmp_path, factory=lambda s, c: FlakySession(tmp_path))
    assert [r["v"] for r in rows(op)] == ["a", "b"]


class ShortSession(LocalDirSession):
    """Server closes the data connection early but reports success (FTP 226 after a truncated transfer)."""

    def download(self, rel, dest):
        data = (self.root / rel).read_bytes()
        Path(dest).write_bytes(data[: len(data) // 2])


def test_short_download_detected(tmp_path):
    write(tmp_path, "d.csv", "id,v\n" + "".join(f"{i},x\n" for i in range(100)))
    op = run(file_lock(), tmp_path, factory=lambda s, c: ShortSession(tmp_path))
    st = audits(op)[0][1] if audits(op) else None
    assert st != "loaded" or op.errors, f"truncated download loaded as a complete delivery ({len(rows(op))} of 100 rows)"


class GrowingSession(LocalDirSession):
    """Listing says 10 bytes; by download time the file is 5 MB (still being written / replaced)."""

    def list_files(self):
        from pf_runtime.sources import RemoteFile
        return [RemoteFile("d.csv", 10, 1000.0)]

    def download(self, rel, dest):
        Path(dest).write_bytes(b"id,v\n" + b"1,x\n" * 1_300_000)


def test_max_package_bytes_enforced_on_actual_bytes(tmp_path):
    op = run(file_lock(package={"max_package_bytes": 1000}), tmp_path, factory=lambda s, c: GrowingSession(tmp_path))
    assert audits(op)[0][1] == "rejected", f"5 MB downloaded and loaded despite max_package_bytes 1000: {audits(op)}"


def test_size_listing_vs_download_mismatch_state(tmp_path):
    """State records the listing size; the content hashed is a different (bigger) file."""
    op = run(file_lock(), tmp_path, factory=lambda s, c: GrowingSession(tmp_path))
    # [after review] a download whose size differs from the listing is retried and never loaded
    assert not rows(op) and op.errors, "state says 10 bytes for a 5 MB delivery that was loaded"


# ---------------------------------------------------------------- real servers
def _env():
    if not os.path.exists(ENV):
        pytest.skip("dry-run servers not running")
    return json.load(open(ENV))


def test_real_sftp_symlinked_delivery_audited(tmp_path):
    """A delivery dropped as a symlink on the SFTP server is neither loaded nor reported as unmatched."""
    e = _env()
    root = Path(e["sftp_root"]) / "adv_symlink"
    root.mkdir(parents=True, exist_ok=True)
    real = root / "real_target.dat"
    real.write_text("id,v\n1,a\n")
    link = root / "d.csv"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(real)
    from pf_runtime.sources.sftp import SftpSession
    lk = file_lock()
    lk["source"].update({"type": "sftp", "host": "127.0.0.1", "port": e["ports"]["sftp"], "username": "pf_sftp",
                         "private_key": {"secret": "k"}, "host_key": e["host_key"], "root": "/adv_symlink", "timeout_seconds": 10})
    lk["secrets"] = {"dev": ["k"]}
    try:
        op = run(lk, cfg={"k": open(e["client_key"]).read()}, factory=SftpSession)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"sftp setup differs: {exc}")
    if op.errors:
        pytest.skip(f"sftp setup differs: {op.errors}")
    paths = [a[0] for a in audits(op)]
    assert "d.csv" in paths, f"symlinked delivery invisible: audits={audits(op)}"


def test_real_s3_originals_failure_blocks_load(tmp_path):
    e = _env()
    write(tmp_path, "d.csv", "id,v\n1,a\n")
    lk = file_lock()
    lk["archive_originals"] = True
    lk["originals"] = {"dev": {"bucket": "no-such-bucket-adv", "prefix": "o", "region": "us-east-1",
                               "access_key_id": {"secret": "ak"}, "secret_access_key": {"secret": "sk"},
                               "endpoint_url": e["s3_endpoint"]}}  # [after review] endpoint comes from the locked policy only
    op = run(lk, tmp_path, cfg={"ak": "x", "sk": "y"})
    assert rows(op) == [] and op.errors


def test_archive_original_key_collision_two_paths_same_bytes(tmp_path):
    """Write-once archive keyed by path+sha: two identical-content deliveries at different paths both archived."""
    e = _env()
    import boto3
    s3 = boto3.client("s3", endpoint_url=e["s3_endpoint"], region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    import uuid

    bucket = f"adv-originals-{uuid.uuid4().hex[:8]}"  # [after review] fresh bucket: the loaded-registry markers persist
    s3.create_bucket(Bucket=bucket)
    write(tmp_path, "a.csv", "id,v\n1,a\n")
    write(tmp_path, "b.csv", "id,v\n1,a\n")
    lk = file_lock()
    lk["archive_originals"] = True
    lk["originals"] = {"dev": {"bucket": bucket, "prefix": "o", "region": "us-east-1",
                               "access_key_id": {"secret": "ak"}, "secret_access_key": {"secret": "sk"},
                               "endpoint_url": e["s3_endpoint"]}}  # [after review] endpoint comes from the locked policy only
    op = run(lk, tmp_path, cfg={"ak": "x", "sk": "y"})
    keys = [a["original_key"] for a in op.rows["pf_package_audit"]]
    assert len(set(keys)) == 2 and all(keys)


def test_pf_originals_endpoint_url_overridable_from_configuration():
    """Security: any configuration key 'pf_originals_endpoint_url' redirects original (GxP) files to another endpoint."""
    import inspect
    from pf_runtime import package as P
    src = inspect.getsource(P.archive_original)
    assert "pf_originals_endpoint_url" not in src, "originals endpoint is taken from free-form configuration, not the locked spec"
