"""Parameter models for every available built-in op (planned ops take free-form params and are rejected later)."""

from __future__ import annotations

from typing import Literal

from .common import StrictModel, ba
from .fieldtypes import ByteSize, Duration, Glob, Identifier, SortedDigests, SortedIds, TemplateStr


class NoParams(StrictModel):
    """This operation takes no parameters."""


class PortalLookupParams(StrictModel):
    endpoint: Identifier = ba("Portal endpoint", "Which portal endpoint (integrations.yaml → portal.endpoints) to call")


class DestinationResolveParams(StrictModel):
    slots: SortedIds = ba("Destination slots", "Which destination slots to resolve, e.g. [x, y]", min_length=1)


class FivetranSyncParams(StrictModel):
    connection: Identifier = ba("Fivetran connection", "Source id of the Fivetran-managed database source")
    wait: bool = ba("Wait for the sync", "Wait until the sync has finished", default=True)
    timeout: Duration = ba("Sync timeout", "How long to wait for the sync", default="2h")


class S3ListParams(StrictModel):
    source: Identifier = ba("Source", "Which source (integrations.yaml → sources) to list")
    recursive: bool = ba("Include sub-folders", "List objects in sub-folders too", default=True)
    include: list[Glob] = ba(
        "Include patterns", "Glob patterns of keys to include, relative to the prefix", default_factory=list
    )
    exclude: list[Glob] = ba("Exclude patterns", "Glob patterns of keys to skip", default_factory=list)
    max_objects: int = ba("Maximum number of objects", "The run fails above this many objects", default=100_000, ge=1)
    max_total_bytes: ByteSize | None = ba("Maximum total size", "The run fails above this total size", default=None)


class S3DownloadParams(StrictModel):
    verify: Literal["size", "size_and_etag"] = ba(
        "Verification", "What to check for each object", default="size_and_etag"
    )
    concurrency: int = ba("Parallel reads", "Objects read in parallel", default=8, ge=1, le=32)


class RestFetchParams(StrictModel):
    api: Identifier = ba("API", "Which API (integrations.yaml → apis) to call")
    endpoint: Identifier = ba("Endpoint", "Which endpoint of that API")
    format: Literal["json", "jsonl", "csv", "binary"] = ba("Format", "How each page is stored", default="json")


class PackageZipParams(StrictModel):
    paths: Literal["relative_to_prefix", "full_key"] = ba(
        "Folder layout in the zip",
        "Keep folders relative to the message prefix, or the full object key",
        default="relative_to_prefix",
    )
    compression: Literal["deflate", "store"] = ba("Compression", "deflate or store", default="deflate")
    level: int = ba("Compression level", "0 (none) to 9 (smallest)", default=6, ge=0, le=9)
    store_if_compressed: bool = ba(
        "Do not re-compress",
        "Store files that are already compressed (zip, gz, png, …) without compressing again",
        default=True,
    )
    zip64: Literal["auto", "always"] = ba("Large zip support", "Use zip64 when needed or always", default="auto")


class ManifestBuildParams(StrictModel):
    file_algorithms: SortedDigests = ba(
        "Per-file checksums", "Checksums listed for every file", default_factory=lambda: ["sha256"]
    )
    package_algorithms: SortedDigests = ba(
        "Package checksums", "Checksums of the whole package", default_factory=lambda: ["md5", "sha256"]
    )
    format: Literal["json"] = ba("Manifest format", "Manifest file format", default="json")


class ChecksumComputeParams(StrictModel):
    algorithms: SortedDigests = ba("Checksums", "Which checksums to compute, e.g. [md5, sha256]", min_length=1)
    compare_with_manifest: bool = ba(
        "Compare with the manifest", "Fail if the re-computed checksums differ from the manifest", default=True
    )


class FileCountParams(StrictModel):
    min: int | None = ba("At least", "Minimum number of files", default=None, ge=0)
    max: int | None = ba("At most", "Maximum number of files", default=None, ge=0)


class TotalSizeParams(StrictModel):
    min: ByteSize | None = ba("At least", "Minimum total size", default=None)
    max: ByteSize | None = ba("At most", "Maximum total size", default=None)


class NamingRenderParams(StrictModel):
    template: TemplateStr = ba("Name template", "e.g. {study_id}_{package_id}.zip")


class DeliverParams(StrictModel):
    slot: Identifier = ba("Destination slot", "Which destination slot (integrations.yaml → destinations)")
    object_name: Identifier = ba("Object name", "The artifact (from a naming rule) that holds the file name")
    manifest: bool = ba("Deliver the manifest", "Deliver the manifest next to the package", default=True)
    sidecars: SortedDigests = ba(
        "Checksum files", "Checksum files delivered next to the package, e.g. [md5, sha256]", default_factory=list
    )
    verify_after_write: bool = ba("Verify after writing", "Re-check the delivered package", default=True)


class StaticPutParams(StrictModel):
    slot: Identifier = ba("Destination slot", "A destination slot with a static resolution")
    object_name: Identifier | None = ba("Object name", "Artifact holding the file name", default=None)


OP_PARAMS: dict[str, type[StrictModel]] = {
    "sqs.read": NoParams,
    "portal.lookup": PortalLookupParams,
    "destination.resolve": DestinationResolveParams,
    "fivetran.sync": FivetranSyncParams,
    "s3.list": S3ListParams,
    "s3.download": S3DownloadParams,
    "rest.fetch": RestFetchParams,
    "package.zip": PackageZipParams,
    "manifest.build": ManifestBuildParams,
    "checksum.compute": ChecksumComputeParams,
    "validate.file_count": FileCountParams,
    "validate.total_size": TotalSizeParams,
    "validate.manifest_match": NoParams,
    "naming.render": NamingRenderParams,
    "destination.deliver": DeliverParams,
    "s3.put": StaticPutParams,
    "s3.copy": StaticPutParams,
    "sftp.put": StaticPutParams,
    "https.put": StaticPutParams,
}
