"""The pipeline bundle: what a pod or Lambda downloads and runs, content-addressed by its sha256 (ADR-0004).

Contents: the six spec files, spec.lock.json, custom rule code (rules/<rule_id>/*) and BUNDLE-MANIFEST.json (sha256 of
every file, the framework and pydantic versions, the hashes from the lock). Written as ZIP_STORED with fixed metadata,
so the bytes and the sha256 do not depend on zlib, the operating system, the clock or the Python version.

Reading is defensive: only the expected names, no traversal, bounded sizes, and every file must match the manifest.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pydantic
from factory_core.errors import ConfigError, SecurityError

from .files import SPEC_DIR, SPEC_FILES
from .lock import LOCK_FILE, build_lock, check_lock

MANIFEST = "BUNDLE-MANIFEST.json"
BUNDLE_VERSION = 1
MAX_MEMBERS = 500
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
_NAME = re.compile(
    rf"^(?:{re.escape(SPEC_DIR)}/[a-z]+\.yaml|{re.escape(LOCK_FILE)}|{re.escape(MANIFEST)}"
    r"|rules/[a-z][a-z0-9_]{0,62}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99})$"
)
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class Bundle:
    data: bytes
    sha256: str
    manifest: dict[str, Any]


def _framework_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("factory-spec")
    except PackageNotFoundError:  # pragma: no cover - source checkout without install
        return "0+unknown"


def _collect(pipeline_dir: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for fname in SPEC_FILES:
        files[f"{SPEC_DIR}/{fname}"] = (pipeline_dir / SPEC_DIR / fname).read_bytes()
    files[LOCK_FILE] = (pipeline_dir / LOCK_FILE).read_bytes()
    rules = pipeline_dir / "rules"
    if rules.is_dir():
        for f in sorted(rules.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                files[f.relative_to(pipeline_dir).as_posix()] = f.read_bytes()
    return files


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name in sorted(files):
            zi = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            zi.create_system = 3
            zi.external_attr = 0o100644 << 16
            zi.compress_type = zipfile.ZIP_STORED
            zf.writestr(zi, files[name])
    return buf.getvalue()


def build_bundle(pipeline_dir: Path) -> Bundle:
    """Bundle a valid pipeline whose spec.lock.json is current (raises ConfigError otherwise)."""
    from .load import load_spec

    res = load_spec(pipeline_dir)
    if res.spec is None:
        raise ConfigError("bundle_spec_invalid", "the spec does not load; run `factory validate`")
    stale = check_lock(res.spec)
    if stale:
        raise ConfigError("bundle_lock_stale", stale[0].msg)
    for name, data in _collect(pipeline_dir).items():
        if not _NAME.match(name):
            raise ConfigError("bundle_name_invalid", f"'{name}' cannot be bundled (names: letters, digits, _ . -)")
        if len(data) > MAX_FILE_BYTES:
            raise ConfigError("bundle_too_large", f"'{name}' is larger than {MAX_FILE_BYTES} bytes")
    files = _collect(pipeline_dir)
    hashes = build_lock(res.spec)["hashes"]
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "pipeline_id": res.spec.pipeline_id,
        "factory_version": _framework_version(),
        "pydantic_version": pydantic.VERSION,
        "logic_sha": hashes["logic_sha"],
        "binding_sha": hashes["binding_sha"],
        "files": {n: hashlib.sha256(d).hexdigest() for n, d in sorted(files.items())},
    }
    files[MANIFEST] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    data = _zip(files)
    return Bundle(data, hashlib.sha256(data).hexdigest(), manifest)


def verify_sha(data: bytes, expected_sha256: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256.lower()):
        raise SecurityError("bundle_hash_mismatch", "the bundle does not match its expected sha256")


def extract_bundle(data: bytes, dest: Path) -> dict[str, Any]:
    """Safely extract a bundle into dest; returns the manifest. Any irregularity is a security error."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SecurityError("bundle_invalid", "the bundle is not a zip archive") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise SecurityError("bundle_invalid", "the bundle is larger than allowed")
        names = [i.filename for i in infos]
        if len(set(names)) != len(names):
            raise SecurityError("bundle_invalid", "the bundle has duplicate names")
        contents: dict[str, bytes] = {}
        for info in infos:
            name = info.filename
            if not _NAME.match(name) or ".." in PurePosixPath(name).parts or info.is_dir():
                raise SecurityError("bundle_invalid", "the bundle contains an unexpected file name")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise SecurityError("bundle_invalid", "the bundle contains a symbolic link")
            if info.compress_type != zipfile.ZIP_STORED or info.file_size > MAX_FILE_BYTES:
                raise SecurityError("bundle_invalid", "the bundle contains an unexpected member")
            contents[name] = zf.read(info)
    raw_manifest = contents.pop(MANIFEST, None)
    if raw_manifest is None:
        raise SecurityError("bundle_invalid", f"the bundle has no {MANIFEST}")
    manifest: dict[str, Any] = json.loads(raw_manifest)
    expected = manifest.get("files", {})
    if set(expected) != set(contents):
        raise SecurityError("bundle_invalid", "the bundle files do not match its manifest")
    for name, blob in contents.items():
        if not hmac.compare_digest(hashlib.sha256(blob).hexdigest(), str(expected[name])):
            raise SecurityError("bundle_invalid", "a bundle file does not match its manifest")
    for name, blob in contents.items():
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return manifest
