"""Spike S6 (local part): streaming a large package keeps memory bounded. Opt-in: `pytest -m spike`.

moto runs in its own process so its memory is not counted; the streaming client runs in another process and reports
its peak RSS (ru_maxrss) and throughput. Size: FACTORY_SPIKE_BYTES (default 2 GB; 10 GB is measured in Phase 8
against real S3, because moto holds whole objects in memory). Synthetic data only.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.spike

CLIENT = r"""
import hashlib, io, json, resource, sys, time
import boto3
from factory_runtime.package.zipstream import DeterministicZip, HashingSink, ZipOptions
from factory_runtime.transports.s3 import MultipartWriter, open_object, part_size_for
endpoint, total = sys.argv[1], int(sys.argv[2])
s3 = boto3.client("s3", endpoint_url=endpoint, region_name="eu-west-1")
objs = sorted(s3.list_objects_v2(Bucket="landing", Prefix="pkg/")["Contents"], key=lambda o: o["Key"])
t0 = time.monotonic()
w = MultipartWriter(s3, "staging", "run/package.zip", part_size=part_size_for(total), concurrency=2)
sink = HashingSink(w)
z = DeterministicZip(sink, ZipOptions(compression="deflate", level=1))
for o in objs:
    with open_object(s3, "landing", o["Key"], if_match=o["ETag"].strip('"'), size=o["Size"]) as fh:
        z.add(o["Key"][4:], o["Size"], fh)
z.close()
w.complete()
secs = time.monotonic() - t0
print(json.dumps({"seconds": round(secs, 1), "source_bytes": sum(o["Size"] for o in objs), "zip_bytes": sink.size,
                  "maxrss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
                  "mb_per_s": round(sum(o["Size"] for o in objs) / secs / 1e6, 1)}))
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_streaming_memory_is_bounded(tmp_path: Path) -> None:
    import boto3
    from factory_testing.synthetic import GeneratedStream

    total = int(float(os.environ.get("FACTORY_SPIKE_BYTES", "2e9")))
    files = 20
    port = _free_port()
    env = {**os.environ, "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing"}
    moto = subprocess.Popen(
        [sys.executable, "-m", "moto.server", "-H", "127.0.0.1", "-p", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        endpoint = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        s3 = boto3.client("s3", endpoint_url=endpoint, region_name="eu-west-1",
                          aws_access_key_id="testing", aws_secret_access_key="testing")  # fmt: skip
        for b in ("landing", "staging"):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        for i in range(files):
            s3.upload_fileobj(GeneratedStream(total // files, seed=i), "landing", f"pkg/part-{i:03d}.bin")
        out = subprocess.run(
            [sys.executable, "-c", CLIENT, endpoint, str(total)], env=env, capture_output=True, text=True, check=True
        )
        report = json.loads(out.stdout.strip().splitlines()[-1])
        (tmp_path / "s6.json").write_text(json.dumps(report, indent=2))
        print("SPIKE-S6", json.dumps(report))  # noqa: T201 - the measurement is the output of a spike
        part = 64 * 1024 * 1024
        assert report["source_bytes"] == (total // files) * files
        # one part filling + two uploading + Python/boto3 baseline (generous)
        assert report["maxrss_mb"] * 1024 * 1024 < part * 3 + 384 * 1024 * 1024
    finally:
        moto.terminate()
        moto.wait(timeout=30)
