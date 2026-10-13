"""s3.copy: server-side copy of the listed objects to a static S3 slot, write-once, keeping their folders.

Each object is copied with UploadPartCopy conditioned on its listed ETag (a source replaced after listing stops the
run, data 65) and completed with If-None-Match, so an existing copy is never replaced. An existing object counts as
ours when it carries the listed source ETag in its metadata (an earlier attempt wrote it) or, failing that, when both
objects hash the same; anything else is a conflict (65). The bytes never pass through the task, so the receipt's
evidence is the conditional copy plus a size check of every copy.
"""

from __future__ import annotations

import hmac
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

from factory_core.errors import ConfigError, DataError, SecurityError
from factory_spec.artifact_models import ObjectEntry, ObjectListing, Receipt, ReceiptFile
from factory_spec.sections.destinations import S3Entry

from .. import delivery
from ..transports.s3 import AlreadyExists, copy_once, head_size, sha256_of
from . import op
from .static_put import static_resolution

ETAG_META = "factory-source-etag"


def _rel_segments(rel_path: str) -> tuple[str, ...]:
    segs = tuple(rel_path.split("/"))
    if any(s in ("", ".", "..") or "\\" in s or "\x00" in s for s in segs):
        raise SecurityError("object_name_unsafe", "a listed object has an unsafe relative path")
    return segs


@op("s3.copy")
def s3_copy(ctx: Any) -> list[Any]:
    res = static_resolution(ctx, ctx.params.slot, "s3")
    entry = res.entry
    if not isinstance(entry, S3Entry):  # pragma: no cover - static_resolution checked the type
        raise ConfigError("destination_type_mismatch", "s3.copy writes to S3 destinations only")
    cfg = ctx.services.config
    listing: ObjectListing = ctx.input_of("object_listing")
    s3 = ctx.services.aws.client("s3")
    owner = delivery.placement_account(cfg, entry)
    owner_kw = {"ExpectedBucketOwner": owner} if owner else {}
    extra = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": delivery.kms_key_id(cfg, entry), **owner_kw}
    bucket = entry.bucket

    def ours(o: ObjectEntry, key: str) -> bool:
        head = head_size(s3, bucket, key, **owner_kw)
        if head is None or head[0] != o.size:
            return False
        tagged = head[1].get(ETAG_META, "")
        if tagged and hmac.compare_digest(tagged, o.etag.strip('"')):
            return True
        return sha256_of(s3, bucket, key, **owner_kw) == sha256_of(s3, listing.bucket, o.key)

    def copy(o: ObjectEntry) -> ReceiptFile:
        key = "/".join((*res.folder, *_rel_segments(o.rel_path)))
        outcome: Literal["written", "already_present"]
        if head_size(s3, bucket, key, **owner_kw) is not None:
            outcome = "already_present"
        else:
            try:
                copy_once(
                    s3, listing.bucket, o.key, bucket, key,
                    size=o.size, src_etag=o.etag, extra=extra, metadata={ETAG_META: o.etag.strip('"')},
                )  # fmt: skip
                outcome = "written"
            except AlreadyExists:
                outcome = "already_present"
        if outcome == "already_present" and not ours(o, key):
            raise DataError(
                "destination_conflict", f"{o.rel_path} already exists at the destination with other content"
            )
        head = head_size(s3, bucket, key, **owner_kw)
        if head is None or head[0] != o.size:
            raise DataError("delivery_verification_failed", f"the copy of {o.rel_path} has the wrong size")
        return ReceiptFile(path=o.rel_path, size=o.size, source_etag=o.etag.strip('"'), outcome=outcome)

    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="s3-copy-obj") as pool:
        files = list(pool.map(copy, listing.objects))
    receipt = Receipt(
        slot=res.slot,
        entry_id=entry.id,
        type="s3",
        target=f"s3://{bucket}/{'/'.join(res.folder)}/",
        object_name="",
        size=sum(f.size for f in files),
        outcome="written" if any(f.outcome == "written" for f in files) else "already_present",
        verification="size_only",
        files=files,
    )
    delivery.record(ctx, receipt)
    return [receipt]
