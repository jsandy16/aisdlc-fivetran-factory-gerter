"""Delivery to approved destinations (ADR-0024), shared by destination.deliver and the static puts.

The allow-list entry is always taken from the bundle's destinations.yaml (re-resolved here, never trusted from a staged
document); credentials, KMS keys and the pinned host key come from that entry. A delivery is:
1. the package (write-once, hash-checked while sending), 2. its verification, 3. the manifest
`<name>.manifest.json`, 4. the checksum sidecars `<name>.md5` / `<name>.sha256` (GNU format) last, so a consumer that
waits for the sidecar never sees a package without its manifest.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from factory_core.errors import ConfigError, DataError, SecurityError
from factory_core.pathmatch import parse_path_pattern
from factory_spec.artifact_models import ChecksumRecord, Manifest, PackageInfo, Receipt
from factory_spec.resolve import Resolution, resolve_destination, target_segments
from factory_spec.sections.destinations import HttpsEntry, S3Entry, SftpEntry

from .config import RuntimeConfig
from .tls import http_client, ssl_context
from .transports.common import Source, Target
from .transports.https import HttpsTarget
from .transports.s3 import S3Target, open_object
from .transports.sftp import SftpTarget

CONTENT_TYPES = {"manifest": "application/json", "sidecar": "text/plain; charset=utf-8"}


def _partition(region: str) -> str:
    if "-gov-" in region:
        return "aws-us-gov"
    if region.startswith("cn-"):
        return "aws-cn"
    return "aws"


def placement_account(cfg: RuntimeConfig, entry: S3Entry) -> str | None:
    """The account owning the entry's bucket; required for any placement other than the team's own account."""
    account = cfg.placement_accounts.get(entry.placement)
    if account is None and entry.placement != "team":
        raise ConfigError("placement_unknown", f"no account is configured for placement '{entry.placement}'")
    return account


def kms_key_id(cfg: RuntimeConfig, entry: S3Entry) -> str:
    """The entry's KMS alias; as an alias ARN in the placement's account when the bucket belongs to another account."""
    if entry.placement == "team":
        return entry.kms_key  # an alias resolves in the caller's (the team's) account
    return f"arn:{_partition(cfg.region)}:kms:{cfg.region}:{placement_account(cfg, entry)}:{entry.kms_key}"


def open_target(ctx: Any, entry: S3Entry | SftpEntry | HttpsEntry, folder: tuple[str, ...]) -> Target:
    rule_id = ctx.rule.rule_id
    if isinstance(entry, S3Entry):
        cfg = ctx.services.config
        return S3Target(
            ctx.services.aws.client("s3"), entry.bucket, folder, kms_key_id(cfg, entry), placement_account(cfg, entry)
        )
    if isinstance(entry, SftpEntry):
        key = ctx.secrets.get(entry.private_key_secret, purpose="sftp delivery", rule_id=rule_id)
        root = tuple(parse_path_pattern(entry.path_pattern, absolute=True).literal_root.split("/"))
        sftp = SftpTarget(entry, folder, key, run_tag=ctx.run.run_key_hash[:12], literal_root=root)
        try:
            sftp.ensure_folder()
        except BaseException:
            sftp.close()
            raise
        return sftp
    bearer = None
    client_cert = None
    if entry.auth.type == "bearer":
        bearer = ctx.secrets.get(entry.auth.token_secret, purpose="https delivery", rule_id=rule_id)
    else:
        client_cert = (
            ctx.secrets.get(entry.auth.cert, purpose="https delivery mTLS", rule_id=rule_id),
            ctx.secrets.get(entry.auth.key, purpose="https delivery mTLS", rule_id=rule_id),
        )
    sslctx = ssl_context(extra_ca=ctx.services.extra_ca_pem, client_cert=client_cert)
    return HttpsTarget(entry, folder, http_client(sslctx, connect_timeout=10, read_timeout=300), bearer)


def recheck(ctx: Any, slot: str, entry_id: str, uri: str) -> Resolution:
    """Resolve a destination again against the bundle's allow-list; the entry must be the expected one."""
    res = resolve_destination(ctx.spec, ctx.env, slot, uri, ctx.vars.strings())
    if res.entry.id != entry_id:
        raise SecurityError("destination_not_allowed", f"slot '{slot}' resolves to another approved destination")
    return res


@dataclass(frozen=True)
class Package:
    info: PackageInfo
    md5: str
    sha256: str

    @classmethod
    def of(cls, info: PackageInfo, checksum: ChecksumRecord | None) -> Package:
        """The digests to deliver: the re-computed ones (checksum.compute) when present; they must agree."""
        md5, sha = info.md5, info.sha256
        if checksum is not None:
            for algo, recorded in (("md5", md5), ("sha256", sha)):
                got = checksum.digests.get(algo)
                if got is not None and not hmac.compare_digest(got, recorded):
                    raise DataError("checksum_mismatch", "the checksum record differs from the package hash record")
        return cls(info, md5, sha)


def sidecar(algo: str, digest: str, name: str) -> bytes:
    """GNU coreutils format (`sha256sum -c` / `md5sum -c` read it): `<hex>  <name>\\n`."""
    return f"{digest}  {name}\n".encode()


def deliver(
    ctx: Any,
    res: Resolution,
    name: str,
    pkg: Package,
    *,
    manifest: Manifest | None,
    sidecars: list[str],
    verify: bool,
) -> Receipt:
    entry = res.entry
    names = [name] + ([f"{name}.manifest.json"] if manifest is not None else []) + [f"{name}.{a}" for a in sidecars]
    for n in names:
        target_segments(res.folder, n)  # each delivered name is one safe file name inside the folder
    s3 = ctx.services.aws.client("s3")
    info = pkg.info
    src = Source(
        info.bucket,
        info.key,
        info.size,
        pkg.sha256,
        lambda: open_object(s3, info.bucket, info.key, size=info.size, what="the staged package"),
    )
    digests = {"md5": pkg.md5, "sha256": pkg.sha256}
    target = open_target(ctx, entry, res.folder)
    try:
        put = target.put_package(name, src, verify=verify)
        if manifest is not None:
            target.put_small(f"{name}.manifest.json", manifest.canonical_json(), CONTENT_TYPES["manifest"])
        for algo in sidecars:
            target.put_small(f"{name}.{algo}", sidecar(algo, digests[algo], name), CONTENT_TYPES["sidecar"])
    finally:
        target.close()
    receipt = Receipt(
        slot=res.slot,
        entry_id=entry.id,
        type=entry.type,
        target=put.target,
        object_name=name,
        size=info.size,
        sha256=pkg.sha256,
        md5=pkg.md5,
        outcome=put.outcome,
        verification=put.verification,
        sidecars=list(sidecars),
        manifest=manifest is not None,
    )
    record(ctx, receipt)
    return receipt


def record(ctx: Any, receipt: Receipt) -> None:
    """The delivery audit event (target in full; the audit store is access-controlled) and the operational signals
    (the log carries only a hash of the target)."""
    ctx.audit.record(
        "delivery",
        rule_id=ctx.rule.rule_id,
        slot=receipt.slot,
        entry_id=receipt.entry_id,
        destination_type=receipt.type,
        target=receipt.target,
        object_name=receipt.object_name,
        outcome=receipt.outcome,
        verification=receipt.verification,
        size=receipt.size,
        sha256=receipt.sha256,
        manifest=receipt.manifest,
        sidecars=receipt.sidecars,
        files=len(receipt.files),
    )
    ctx.log(
        "delivered",
        slot=receipt.slot,
        destination_type=receipt.type,
        target_sha256=hashlib.sha256(receipt.target.encode()).hexdigest()[:16] if receipt.target else None,
        outcome=receipt.outcome,
        verification=receipt.verification,
        bytes=receipt.size,
    )
    if receipt.verification == "none" and receipt.outcome != "skipped":
        ctx.log("delivery_unverified", level="WARNING", slot=receipt.slot, destination_type=receipt.type)
    if receipt.outcome == "written" and receipt.type is not None:
        ctx.services.log.metric("BytesTransferred", receipt.size, "Bytes", destination_type=receipt.type)


def skipped(ctx: Any, slot: str, name: str) -> Receipt:
    """An optional slot the portal left empty: nothing is written, and the receipt says so."""
    receipt = Receipt(
        slot=slot,
        entry_id=None,
        type=None,
        target="",
        object_name=name,
        size=0,
        outcome="skipped",
        verification="none",
    )
    record(ctx, receipt)
    return receipt
