"""Versioned, immutable release artefacts.

`pf release <p>` (after a clean build) produces artifacts/<p>/<version>/:
  connector.zip   deterministic zip of build/connector (sorted, fixed timestamps)
  release.json    versions, hashes of every file, spec/rules lock hashes, git commit
and records pipelines/<p>/releases/<version>.json (committed) with the lock, so later changes can be classified.
A version can never be re-released with different content. `--check` enforces semantic versioning:
breaking change (entity/attribute removed, type/key/load changed) -> major; additions or rule changes -> minor;
documentation only -> patch.
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

from .bundle import Bundle
from .envelope import PfError


def _zip(dir_: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(dir_.rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts or p.name.startswith(".") or "files" in p.relative_to(dir_).parts[:1] \
                    or p.name == "configuration.json":
                continue
            info = zipfile.ZipInfo(p.relative_to(dir_).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, p.read_bytes())
    return buf.getvalue()


def _git_commit(root: Path) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _semver(v: str) -> tuple[int, int, int]:
    a, b, c = v.split(".")
    return int(a), int(b), int(c)


def _pseudonymised(attr: dict) -> bool:
    return any(t.get("kind") == "pseudonymise" for t in attr.get("transforms") or [])


def requires_resync(old: dict, new: dict) -> bool:
    """Primary key or load mode changed: Fivetran needs the table dropped and a full historical re-sync."""
    oe = {e["name"]: e for e in old["entities"]}
    return any(e["name"] in oe and (oe[e["name"]]["primary_key"] != e["primary_key"] or oe[e["name"]]["load"] != e["load"])
               for e in new["entities"])


def classify(old: dict, new: dict) -> tuple[str, list[str]]:
    """Return (required bump, reasons) comparing two locks."""
    reasons_major, reasons_minor = [], []
    oe = {e["name"]: e for e in old["entities"]}
    ne = {e["name"]: e for e in new["entities"]}
    for n in oe:
        if n not in ne:
            reasons_major.append(f"entity {n} removed")
            continue
        a, b = oe[n], ne[n]
        if a["primary_key"] != b["primary_key"]:
            reasons_major.append(f"{n}: primary key changed")
        if a["load"] != b["load"]:
            reasons_major.append(f"{n}: load mode changed")
        oa = {x["name"]: x for x in a["attributes"]}
        na = {x["name"]: x for x in b["attributes"]}
        for an in oa:
            if an not in na:
                reasons_major.append(f"{n}.{an} removed")
            elif (oa[an]["type"], oa[an].get("precision"), oa[an].get("scale")) != (na[an]["type"], na[an].get("precision"), na[an].get("scale")):
                reasons_major.append(f"{n}.{an} type changed")
            elif an in b["primary_key"] and (oa[an].get("transforms"), oa[an].get("source")) != (na[an].get("transforms"), na[an].get("source")):
                reasons_major.append(f"{n}.{an}: key values change (transform or source of a primary-key column)")
            elif _pseudonymised(oa[an]) != _pseudonymised(na[an]):
                reasons_major.append(f"{n}.{an}: pseudonymisation added or removed")
        for an in na:
            if an not in oa:
                reasons_minor.append(f"{n}.{an} added")
    for n in ne:
        if n not in oe:
            reasons_minor.append(f"entity {n} added")
    strip = lambda d: json.dumps({k: v for k, v in d.items() if k not in ("version", "spec_sha256", "rules_lock_sha256", "title")}, sort_keys=True)  # noqa: E731
    if not reasons_major and not reasons_minor and strip(old) != strip(new):
        reasons_minor.append("rules, transforms or settings changed")
    if reasons_major:
        return "major", reasons_major + reasons_minor
    if reasons_minor:
        return "minor", reasons_minor
    if old["spec_sha256"] != new["spec_sha256"]:
        return "patch", ["documentation or descriptions changed"]
    return "none", []


def _records(b: Bundle) -> list[tuple[tuple[int, int, int], str, dict]]:
    d = b.path / "releases"
    recs = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            recs.append((_semver(f.stem), f.stem, rec["lock"], rec.get("release", {})))
        except (ValueError, KeyError, TypeError):
            # a release record is evidence: an unreadable one is an error, never skipped
            raise PfError(f"release record {f.relative_to(b.path)} is unreadable; restore it from Git history", "corrupt_release") from None
    return sorted(recs, key=lambda r: r[0])


def check_version(b: Bundle, lock: dict, require_record: bool = False, connector_sha: str | None = None) -> dict:
    """Semantic-version gate. With require_record (CI), the release record for this version must be committed."""
    cur = _semver(lock["version"])
    recs = _records(b)
    same = next((r for r in recs if r[0] == cur), None)
    if same is not None:
        if same[2]["spec_sha256"] != lock["spec_sha256"] or same[2].get("rules_lock_sha256") != lock.get("rules_lock_sha256") \
                or same[2].get("framework_version") != lock.get("framework_version") or same[2].get("runtime_version") != lock.get("runtime_version") \
                or (connector_sha and same[3].get("connector_zip_sha256") not in (None, connector_sha)):
            older = [r for r in recs if r[0] < cur]
            need = classify(older[-1][2], lock)[0] if older else "minor"
            return {"ok": False, "msg": f"version {lock['version']} is already released with different content; bump the version ({need} or higher)"}
    elif require_record:
        return {"ok": False, "msg": f"no release record for {lock['version']}: run `pf release {b.name}` and commit releases/{lock['version']}.json"}
    older = [r for r in recs if r[0] < cur]
    newer = [r for r in recs if r[0] > cur]
    if newer:
        return {"ok": False, "msg": f"version {lock['version']} is lower than the released {newer[-1][1]}"}
    if not older:
        return {"ok": True, "previous": None, "required": "initial"}
    pv, plock = older[-1][1], older[-1][2]
    need, reasons = classify(plock, lock)
    old = _semver(pv)
    bumped = "major" if cur[0] > old[0] else "minor" if cur[1] > old[1] else "patch"
    order = {"none": 0, "patch": 1, "minor": 2, "major": 3}
    ok = order[bumped] >= order[need]
    return {"ok": ok, "previous": pv, "required": need, "bumped": bumped, "reasons": reasons,
            **({} if ok else {"msg": f"this change needs a {need} version bump (found {bumped}): {'; '.join(reasons)}"})}


def release(root: Path, b: Bundle, upload: str | None = None, check_only: bool = False) -> dict:
    build = root / "pipelines" / b.name / "build"
    if not (build / "spec.lock.json").exists():
        raise PfError("no build found; run `pf build` first", "not_built")
    lock = json.loads((build / "spec.lock.json").read_text(encoding="utf-8"))
    data = _zip(build / "connector")
    ver = check_version(b, lock, require_record=check_only, connector_sha=hashlib.sha256(data).hexdigest())
    if not ver["ok"]:
        raise PfError(ver["msg"], "version")
    if check_only:
        return ver
    older = [r for r in _records(b) if r[0] < _semver(lock["version"])]
    older_lock = older[-1][2] if older else None
    manifest = json.loads((build / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
    rec = {
        "pipeline": b.name, "version": lock["version"], "framework_version": lock["framework_version"],
        "runtime_version": lock["runtime_version"], "spec_sha256": lock["spec_sha256"], "rules_lock_sha256": lock["rules_lock_sha256"],
        "connector_zip_sha256": hashlib.sha256(data).hexdigest(), "build_files": manifest["files"], "git_commit": _git_commit(root),
        "requires_resync": bool(older_lock and requires_resync(older_lock, lock)),
    }
    out = root / "artifacts" / b.name / lock["version"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "connector.zip").write_bytes(data)
    (out / "release.json").write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    recf = b.path / "releases" / f"{lock['version']}.json"
    recf.parent.mkdir(exist_ok=True)
    if not recf.exists():
        recf.write_text(json.dumps({"release": {k: v for k, v in rec.items() if k != "build_files"}, "lock": lock}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result = {"version": lock["version"], "artifact_dir": str(out.relative_to(root)), "connector_zip_sha256": rec["connector_zip_sha256"], **ver}
    if upload:
        result["upload"] = _upload(upload, b.name, lock["version"], data, rec)
    return result


def _upload(target: str, name: str, version: str, data: bytes, rec: dict) -> dict:
    import boto3
    from botocore.exceptions import ClientError

    if not target.startswith("s3://"):
        raise PfError("--upload must be s3://bucket[/prefix]", "invalid")
    bucket, _, prefix = target[5:].partition("/")
    s3 = boto3.client("s3")
    done = {}
    for fname, body in (("connector.zip", data), ("release.json", (json.dumps(rec, indent=2, sort_keys=True) + "\n").encode())):
        key = "/".join(x for x in (prefix.strip("/"), name, version, fname) if x)
        sha = hashlib.sha256(body).hexdigest()
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*", ChecksumAlgorithm="SHA256", Metadata={"pf-sha256": sha})
            done[fname] = "uploaded"
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            head = s3.head_object(Bucket=bucket, Key=key)
            if head.get("Metadata", {}).get("pf-sha256") != sha:
                raise PfError(f"s3://{bucket}/{key} already holds different content for version {version}", "immutable") from None
            done[fname] = "already present (identical)"
    return done
