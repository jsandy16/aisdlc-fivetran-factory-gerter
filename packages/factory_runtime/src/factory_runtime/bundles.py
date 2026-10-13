"""Fetch, verify and load a pipeline bundle (ADR-0004).

The bundle location is fixed by configuration (s3://<bundle store>/<team>/<pipeline_id>/<sha256>.zip), so a task can
only run code its own pipeline published. The sha256 is checked before anything is parsed (mismatch: security, 77).
The extracted spec is loaded with the same loader as `factory validate`, and its freshly computed lock must equal the
bundled spec.lock.json: a different framework version would otherwise run the spec with other semantics (config, 78).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory_core.errors import ConfigError, SecurityError
from factory_spec.bundle import extract_bundle, verify_sha
from factory_spec.load import load_spec
from factory_spec.lock import LOCK_FILE, build_lock, lock_text
from factory_spec.spec import PipelineSpec

from .aws import AwsClients
from .config import RuntimeConfig

_OK = ".factory-bundle-ok"


@dataclass(frozen=True)
class LoadedBundle:
    sha256: str
    spec: PipelineSpec
    manifest: dict[str, Any]
    path: Path
    hashes: dict[str, Any]


def bundle_uri(config: RuntimeConfig, sha256: str) -> str:
    return f"s3://{config.bundle_store}/{config.bundle_prefix}{sha256}.zip"


def _location(config: RuntimeConfig, uri: str, sha256: str) -> tuple[str, str]:
    expected = bundle_uri(config, sha256)
    if uri != expected:
        raise SecurityError("bundle_location_invalid", "the bundle is not in this pipeline's bundle store location")
    rest = uri.removeprefix("s3://")
    bucket, _, key = rest.partition("/")
    return bucket, key


def load_bundle(aws: AwsClients, config: RuntimeConfig, uri: str, sha256: str) -> LoadedBundle:
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise SecurityError("bundle_hash_invalid", "the bundle sha256 is not a lower-case hex digest")
    bucket, key = _location(config, uri, sha256)
    cache = config.workdir / "bundles" / sha256
    if not (cache / _OK).is_file():
        data = aws.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        verify_sha(data, sha256)
        (config.workdir / "bundles").mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=config.workdir / "bundles"))
        try:
            extract_bundle(data, tmp)
            (tmp / _OK).write_text(sha256)
            if cache.exists():
                shutil.rmtree(cache)
            tmp.rename(cache)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    manifest = (
        json.loads((cache / "BUNDLE-MANIFEST.json").read_text()) if (cache / "BUNDLE-MANIFEST.json").exists() else {}
    )
    res = load_spec(cache)
    if res.spec is None:
        raise ConfigError("bundle_spec_invalid", "the bundled spec does not load with this runtime")
    bundled = (cache / LOCK_FILE).read_text(encoding="utf-8")
    if lock_text(res.spec) != bundled:
        raise ConfigError(
            "bundle_framework_mismatch",
            "the bundled spec.lock.json differs from this runtime's reading of the spec (framework version skew)",
        )
    if res.spec.pipeline_id != config.pipeline_id:
        raise ConfigError("bundle_pipeline_mismatch", "the bundle belongs to another pipeline")
    return LoadedBundle(sha256, res.spec, manifest, cache, build_lock(res.spec)["hashes"])
