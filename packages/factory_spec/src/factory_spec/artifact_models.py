"""Canonical JSON documents the runtime stages, delivers and audits (one model per artifact kind).

All are strict and frozen; sizes are integers; no floats (canonical JSON refuses them). Staged copies are written
write-once as canonical JSON, so a retry that produces the same document compares equal byte for byte.
"""

from __future__ import annotations

from typing import Any, Literal

from factory_core.canonical import canonical_bytes, sha256_hex
from pydantic import BaseModel, ConfigDict, Field, JsonValue

Digest = Literal["md5", "sha256", "sha512"]


class Doc(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def canonical_json(self) -> bytes:
        return canonical_bytes(self.model_dump(mode="json"))

    def doc_sha256(self) -> str:
        return sha256_hex(self.canonical_json())


class TriggerSnapshot(Doc):
    """Custody snapshot of what started the run: the validated body only (duplicates of one event compare equal)."""

    kind: Literal["trigger_snapshot"] = "trigger_snapshot"
    source: Literal["sqs", "s3_event", "schedule", "manual"]
    body: dict[str, JsonValue]
    body_sha256: str


class ObjectEntry(Doc):
    key: str
    rel_path: str
    size: int = Field(ge=0)
    etag: str
    version_id: str | None = None


class ObjectListing(Doc):
    kind: Literal["object_listing"] = "object_listing"
    bucket: str
    prefix: str
    objects: list[ObjectEntry]
    count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)


class PortalSnapshot(Doc):
    """The mapped answer only (never the raw payload): one URI per slot and the declared values."""

    kind: Literal["portal_snapshot"] = "portal_snapshot"
    endpoint: str
    destinations: dict[str, str | None]
    values: dict[str, JsonValue]


class ResolvedDestination(Doc):
    slot: str
    entry_id: str
    type: Literal["s3", "sftp", "https"]
    uri: str


class DestinationSet(Doc):
    kind: Literal["destination_set"] = "destination_set"
    env: str
    destinations: dict[str, ResolvedDestination]


class PackageMember(Doc):
    path: str
    size: int = Field(ge=0)
    sha256: str
    md5: str | None = None
    compression: Literal["deflate", "store"]


class PackageInfo(Doc):
    """The hash record of a staged package, written before the package itself is completed."""

    kind: Literal["package"] = "package"
    bucket: str
    key: str
    size: int = Field(ge=0)
    md5: str
    sha256: str
    members: list[PackageMember]
    zip64: bool
    zlib_version: str


class ManifestFile(Doc):
    path: str
    size: int = Field(ge=0)
    sha256: str
    md5: str | None = None


class ManifestPackage(Doc):
    size: int = Field(ge=0)
    md5: str | None = None
    sha256: str | None = None


class Manifest(Doc):
    kind: Literal["manifest"] = "manifest"
    manifest_version: Literal[1] = 1
    pipeline_id: str
    run: str  # run_key hash, never the run key itself
    source: dict[str, str]
    package: ManifestPackage
    files: list[ManifestFile]
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)


class ChecksumRecord(Doc):
    kind: Literal["checksum"] = "checksum"
    size: int = Field(ge=0)
    digests: dict[str, str]
    compared_with_manifest: bool


class ReceiptFile(Doc):
    """One object of a multi-object delivery (s3.copy): its path below the target folder."""

    path: str
    size: int = Field(ge=0)
    source_etag: str
    outcome: Literal["written", "already_present"]


class Receipt(Doc):
    """Evidence of one delivery: what was written where, how it was verified.

    `skipped`: an optional slot the portal left empty (nothing was written). `verification: none`: the destination
    offered no way to check the written file (decision P3-3); the receipt says so rather than claiming a check.
    A multi-object copy lists its objects in `files`; `sha256` is then absent (the copy is server-side).
    """

    kind: Literal["receipt"] = "receipt"
    slot: str
    entry_id: str | None
    type: Literal["s3", "sftp", "https"] | None
    target: str
    object_name: str
    size: int = Field(ge=0)
    sha256: str | None = None
    md5: str | None = None
    outcome: Literal["written", "already_present", "skipped"]
    verification: Literal["read_back", "digest", "size_only", "server_checksum", "none"]
    sidecars: list[str] = Field(default_factory=list)
    manifest: bool = False
    files: list[ReceiptFile] = Field(default_factory=list)


class SyncReport(Doc):
    """What a Fivetran sync did: which connection, whether this run started it, when it finished (fivetran.sync)."""

    kind: Literal["report"] = "report"
    report_type: Literal["fivetran_sync"] = "fivetran_sync"
    connection: str
    connection_id: str
    outcome: Literal["succeeded", "triggered"]
    triggered: bool
    previous_succeeded_at: str | None = None
    succeeded_at: str | None = None


ARTIFACT_MODELS: dict[str, type[Doc]] = {
    "message": TriggerSnapshot,
    "object_listing": ObjectListing,
    "portal_snapshot": PortalSnapshot,
    "destination_set": DestinationSet,
    "package": PackageInfo,
    "manifest": Manifest,
    "checksum": ChecksumRecord,
    "receipt": Receipt,
    "report": SyncReport,
}


def facet(kind: str, doc: Any) -> Any:
    """What an expression sees of an artifact (`artifacts.<name>` in CEL): small, typed, no file contents."""
    if kind == "value":
        return doc
    if isinstance(doc, TriggerSnapshot):
        return dict(doc.body)
    if isinstance(doc, ObjectListing):
        return {"count": doc.count, "total_bytes": doc.total_bytes}
    if isinstance(doc, PortalSnapshot):
        return {"values": dict(doc.values), "slots": sorted(k for k, v in doc.destinations.items() if v)}
    if isinstance(doc, DestinationSet):
        return {"slots": sorted(doc.destinations), "types": {k: v.type for k, v in doc.destinations.items()}}
    if isinstance(doc, PackageInfo):
        return {"size": doc.size, "member_count": len(doc.members), "sha256": doc.sha256, "md5": doc.md5}
    if isinstance(doc, Manifest):
        return {"file_count": doc.file_count, "total_bytes": doc.total_bytes, "package_size": doc.package.size}
    if isinstance(doc, ChecksumRecord):
        return {"size": doc.size, **doc.digests}
    if isinstance(doc, SyncReport):
        return {"outcome": doc.outcome, "succeeded_at": doc.succeeded_at}
    if isinstance(doc, Receipt):
        return {"outcome": doc.outcome, "verification": doc.verification, "size": doc.size}
    return {}
