"""Deliveries (packages): discover, order, check readiness, download, archive the original, unpack safely,
and run structural checks — all before a single row is emitted."""
from __future__ import annotations

import gzip
import hashlib
import os
import posixpath
import re
import shutil
import tempfile
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Any

from . import rules as R
from .errors import ConfigError, PackageRejected
from .sources import RemoteFile


@dataclass
class Package:
    file: RemoteFile
    groups: dict[str, str]
    order_key: tuple

    @property
    def path(self) -> str:
        return self.file.path


@dataclass
class LocalPackage:
    pkg: Package
    workdir: str
    local_path: str
    sha256: str
    md5: str
    members: dict[str, str] = field(default_factory=dict)  # member path -> local file

    def facts(self) -> dict[str, Any]:
        return {
            "name": posixpath.basename(self.pkg.path),
            "path": self.pkg.path,
            "folder": posixpath.dirname(self.pkg.path),
            "size": self.pkg.file.size,
            "sha256": self.sha256,
            "members": sorted(self.members),
            "groups": dict(self.pkg.groups),
        }

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


def _order_part(v: str | None) -> tuple:
    if v is None:
        return (2, "")
    return (0, int(v)) if v.isdigit() else (1, v)


def sidecar_names(spec: dict, name: str) -> set[str]:
    out = set()
    for c in spec.get("checks", []):
        if c["kind"] == "checksum":
            out.add(c["sidecar"].format(name=name, algorithm=c["algorithm"]))
    marker = (spec.get("readiness") or {}).get("marker")
    if marker:
        out.add(marker.format(name=name))
    return out


def discover(files: list[RemoteFile], spec: dict, now: float | None = None) -> tuple[list[Package], int, list[str]]:
    """Return (ready packages in order, not-ready count, ignored paths). Sidecars/markers are never packages."""
    now = now or time.time()
    rx = re.compile(spec["match"])
    names = {f.path for f in files}
    helpers: set[str] = set()
    candidates: list[tuple[RemoteFile, re.Match]] = []
    for f in files:
        m = rx.fullmatch(f.path)
        if m:
            candidates.append((f, m))
    for f, _ in candidates:
        d = posixpath.dirname(f.path)
        for h in sidecar_names(spec, posixpath.basename(f.path)):
            helpers.add(posixpath.join(d, h) if d else h)
    ready: list[Package] = []
    not_ready = 0
    readiness = spec.get("readiness") or {}
    for f, m in candidates:
        if f.path in helpers:
            continue
        if now - f.mtime < readiness.get("min_age_seconds", 300):
            not_ready += 1
            continue
        marker = readiness.get("marker")
        if marker:
            d = posixpath.dirname(f.path)
            mk = marker.format(name=posixpath.basename(f.path))
            if (posixpath.join(d, mk) if d else mk) not in names:
                not_ready += 1
                continue
        groups = {k: v for k, v in m.groupdict().items() if v is not None}
        key = tuple(_order_part(groups.get(g)) for g in spec.get("order_by", [])) + ((1, f.path),)
        ready.append(Package(f, groups, key))
    ready.sort(key=lambda p: p.order_key)
    ignored = sorted(n for n in names if n not in {p.path for p in ready} and n not in helpers and not rx.fullmatch(n))
    return ready, not_ready, ignored


def hash_file(path: str) -> tuple[str, str]:
    sha, md5 = hashlib.sha256(), hashlib.md5()  # md5 only to compare vendor sidecars
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def sniff(path: str) -> str:
    with open(path, "rb") as fh:
        head = fh.read(4096)
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    text = head.lstrip(b"\xef\xbb\xbf").lstrip()
    if text.startswith(b"["):
        return "json"
    if text.startswith(b"{"):
        return "jsonl" if b"\n{" in text or text.count(b"\n") <= 1 else "json"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    return "csv" if (b"," in head or b"\t" in head or b"|" in head or b";" in head) else "text"


# ---------------------------------------------------------------------------- archive originals (write-once)
def archive_original(lp: LocalPackage, originals: dict, configuration: dict, pipeline: str) -> str:
    """Put the exact bytes into the originals bucket with If-None-Match: * (never overwrites).
    Returns the S3 key. A 412 means the same content is already archived — success."""
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    key_id = configuration.get(originals["access_key_id"]["secret"])
    secret = configuration.get(originals["secret_access_key"]["secret"])
    if not key_id or not secret:
        raise ConfigError("originals bucket credentials are missing from the configuration")
    s3 = boto3.client(
        "s3", region_name=originals["region"], aws_access_key_id=key_id, aws_secret_access_key=secret,
        endpoint_url=configuration.get("pf_originals_endpoint_url") or None,
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )
    key = f"{originals.get('prefix', 'originals').strip('/')}/{pipeline}/{lp.pkg.path}/{lp.sha256}"
    try:
        with open(lp.local_path, "rb") as fh:
            s3.put_object(Bucket=originals["bucket"], Key=key, Body=fh, IfNoneMatch="*", ChecksumAlgorithm="SHA256",
                          Metadata={"pf-sha256": lp.sha256, "pf-source-path": lp.pkg.path[:1000]})
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 412 or code == "PreconditionFailed":
            return key
        raise
    return key


# ---------------------------------------------------------------------------- unpack
def _safe_member(name: str) -> str | None:
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or not parts:
        return None
    return "/".join(parts)


def unpack(lp: LocalPackage, transforms: list[dict]) -> None:
    """Apply package transforms. Members start as the file itself; unzip/gunzip replace them."""
    lp.members = {posixpath.basename(lp.pkg.path): lp.local_path}
    for t in transforms:
        if t["kind"] == "unzip":
            lp.members = _unzip(lp, t)
        elif t["kind"] == "gunzip":
            lp.members = _gunzip(lp, t)
        elif t["kind"] == "rename_members":
            rx = re.compile(t["pattern"])
            renamed = {}
            for name, local in lp.members.items():
                m = rx.fullmatch(name)
                new = t["to"].format(**m.groupdict()) if m else name
                if new in renamed:
                    raise PackageRejected("rename_members", "two members renamed to the same name")
                renamed[new] = local
            lp.members = renamed


def _unzip(lp: LocalPackage, t: dict) -> dict[str, str]:
    try:
        return _unzip_all(lp, t)
    except (zipfile.BadZipFile, EOFError, zlib.error, NotImplementedError) as exc:  # corrupt, encrypted or unsupported
        raise PackageRejected("unzip", f"unreadable archive ({type(exc).__name__})") from None


def _unzip_all(lp: LocalPackage, t: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for src_name, src_local in list(lp.members.items()):
        if sniff(src_local) != "zip":
            raise PackageRejected("unzip", "member is not a zip archive")
        try:
            zf = zipfile.ZipFile(src_local)
        except zipfile.BadZipFile:
            raise PackageRejected("unzip", "corrupt zip archive") from None
        with zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > t["max_members"]:
                raise PackageRejected("unzip", f"{len(infos)} members exceed max_members {t['max_members']}")
            total = 0
            for info in infos:
                name = _safe_member(info.filename)
                if name is None:
                    raise PackageRejected("unzip", "member path escapes the archive (absolute or ..)")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise PackageRejected("unzip", "symbolic links are not allowed in packages")
                if name in out:
                    raise PackageRejected("unzip", "duplicate member path")
                if info.compress_size and info.file_size / max(info.compress_size, 1) > t["max_ratio"]:
                    raise PackageRejected("unzip", "compression ratio exceeds max_ratio (zip-bomb guard)")
                if sniff_zip_member(zf, info) == "zip":
                    raise PackageRejected("unzip", "nested archives are not allowed")
                dest = os.path.join(lp.workdir, "m", hashlib.sha256(name.encode()).hexdigest()[:24])
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                written = 0
                limit = max(info.compress_size, 1) * t["max_ratio"]  # enforced on real bytes: headers can lie
                with zf.open(info) as src, open(dest, "wb") as dst:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        written += len(chunk)
                        total += len(chunk)
                        if total > t["max_total_bytes"]:
                            raise PackageRejected("unzip", "uncompressed size exceeds max_total_bytes")
                        if written > limit and written > (1 << 20):
                            raise PackageRejected("unzip", "compression ratio exceeds max_ratio (zip-bomb guard)")
                        dst.write(chunk)
                out[name] = dest
    return out


def sniff_zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    with zf.open(info) as fh:
        head = fh.read(4)
    return "zip" if head.startswith(b"PK\x03\x04") else "other"


def _gunzip(lp: LocalPackage, t: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, local in lp.members.items():
        new = name[:-3] if name.endswith(".gz") else name + ".out"
        dest = os.path.join(lp.workdir, "g", hashlib.sha256(new.encode()).hexdigest()[:24])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        total = 0
        try:
            with gzip.open(local, "rb") as src, open(dest, "wb") as dst:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    total += len(chunk)
                    if total > t["max_total_bytes"]:
                        raise PackageRejected("gunzip", "uncompressed size exceeds max_total_bytes")
                    dst.write(chunk)
        except (OSError, EOFError):
            raise PackageRejected("gunzip", "corrupt gzip stream") from None
        out[new] = dest
    return out


# ---------------------------------------------------------------------------- structural checks
RECEIVE_KINDS = {"file_name_pattern", "file_extension", "folder_structure", "file_size", "not_empty", "duplicate_package", "checksum", "package_type"}
MEMBER_KINDS = {"required_members", "forbidden_members", "member_count", "encoding", "expression", "python", "english"}


def run_receive_checks(lp: LocalPackage, checks: list[dict], stats: R.Stats, session, seen_hashes: set[str]) -> None:
    name = posixpath.basename(lp.pkg.path)
    for c in checks:
        k = c["kind"]
        if k not in RECEIVE_KINDS:
            continue
        ok, reason = True, ""
        if k == "file_name_pattern":
            ok, reason = re.fullmatch(c["pattern"], name) is not None, "file name does not match the naming convention"
        elif k == "file_extension":
            ok, reason = any(name.lower().endswith(e.lower()) for e in c["allowed"]), "file extension not allowed"
        elif k == "folder_structure":
            ok, reason = re.fullmatch(c["path_pattern"], posixpath.dirname(lp.pkg.path)) is not None, "folder does not match the expected structure"
        elif k == "file_size":
            size = os.path.getsize(lp.local_path)
            ok = size >= c.get("min_bytes", 1) and (c.get("max_bytes") is None or size <= c["max_bytes"])
            reason = "file size outside the agreed bounds"
        elif k == "not_empty":
            ok, reason = os.path.getsize(lp.local_path) > 0, "empty delivery"
        elif k == "duplicate_package":
            ok, reason = lp.sha256 not in seen_hashes, "identical content was already loaded"
        elif k == "checksum":
            sidecar = c["sidecar"].format(name=name, algorithm=c["algorithm"])
            d = posixpath.dirname(lp.pkg.path)
            text = session.read_text(posixpath.join(d, sidecar) if d else sidecar)
            expected = (text or "").strip().split()[0].lower() if text and text.strip() else None
            actual = lp.sha256 if c["algorithm"] == "sha256" else lp.md5
            ok = expected == actual
            reason = "checksum sidecar missing" if expected is None else "checksum does not match the sidecar"
        elif k == "package_type":
            ok, reason = sniff(lp.local_path) in c["allowed"], "package type not allowed"
        R.package_check(c, stats, ok, reason)


def run_member_checks(lp: LocalPackage, checks: list[dict], stats: R.Stats) -> None:
    members = sorted(lp.members)
    for c in checks:
        k = c["kind"]
        if k not in MEMBER_KINDS:
            continue
        ok, reason = True, ""
        if k == "required_members":
            missing = [p for p in c["patterns"] if not any(re.fullmatch(p, m) for m in members)]
            ok, reason = not missing, f"{len(missing)} required member pattern(s) not found"
        elif k == "forbidden_members":
            ok, reason = not any(re.fullmatch(p, m) for p in c["patterns"] for m in members), "forbidden member present"
        elif k == "member_count":
            n = len(members)
            ok = n >= c.get("min", 1) and (c.get("max") is None or n <= c["max"])
            reason = f"{n} members outside the expected count"
        elif k == "encoding":
            ok, reason = all(_decodes(lp.members[m], c.get("encoding", "utf-8")) for m in members), "member not in the declared encoding"
        else:  # expression / python / english over package facts
            ok = R.check_passes(c, None, None, package=lp.facts())
            reason = "custom package check failed"
        R.package_check(c, stats, ok, reason)


def _decodes(path: str, encoding: str) -> bool:
    import codecs

    dec = codecs.getincrementaldecoder(encoding)()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                dec.decode(chunk)
            dec.decode(b"", final=True)
        return True
    except UnicodeDecodeError:
        return False


def new_workdir() -> str:
    return tempfile.mkdtemp(prefix="pfpkg_")


def control_count(lp: LocalPackage, check: dict) -> int | None:
    rx = re.compile(check["control_member"])
    for m, local in lp.members.items():
        if rx.fullmatch(m):
            with open(local, "rb") as fh:
                text = fh.read(1 << 16).decode("utf-8", errors="replace")
            found = re.search(check["count_pattern"], text)
            return int(found.group(1)) if found else None
    return None

