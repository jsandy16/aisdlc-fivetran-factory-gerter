"""s3.list: list a source folder (from the trigger message or a static binding) within the bound allow-list.

The bucket must be bound for the environment and the folder must lie under a bound prefix root; the message cannot
point the run at anything else (security, 77). A folder is a prefix ending in "/": without it `incoming/PKG1` would
also list `incoming/PKG10/...`. Keys with traversal or control characters stop the run (data, 65). Folder markers
are skipped; include/exclude use the factory glob dialect; limits are enforced. The listing is staged and, on a
retry, the first listing is adopted, so the package always contains exactly what was listed.
"""

from __future__ import annotations

from typing import Any

from factory_core.errors import ConfigError, DataError, SecurityError
from factory_core.globmatch import glob_match
from factory_spec.artifact_models import ObjectEntry, ObjectListing

from . import op


def _source_location(ctx: Any) -> tuple[str, str]:
    src_id = ctx.params.source
    src = next((s for s in ctx.spec.integrations.sources if s.id == src_id), None)
    binding = ctx.binding.sources.get(src_id)
    if src is None or src.type != "s3" or binding is None:
        raise ConfigError("source_unbound", f"S3 source '{src_id}' is not declared or not bound in {ctx.env}")
    if src.location.via == "static":
        if binding.bucket is None:
            raise ConfigError("source_unbound", f"S3 source '{src_id}' has no bucket in {ctx.env}")
        return binding.bucket, src.location.prefix
    body = ctx.vars.trigger.body if ctx.vars.trigger is not None else {}
    bucket = body.get(src.location.bucket_field)
    prefix = body.get(src.location.prefix_field)
    if not isinstance(bucket, str) or not isinstance(prefix, str):
        raise DataError("source_location_missing", "the trigger did not name the source bucket and folder")
    if bucket not in binding.buckets:
        raise SecurityError("source_bucket_not_allowed", "the trigger names a bucket this pipeline may not read")
    if not prefix.endswith("/") or "//" in prefix or prefix.startswith("/") or ".." in prefix.split("/"):
        raise DataError("source_prefix_invalid", "the trigger's folder must be a clean prefix ending with '/'")
    if not any(prefix.startswith(root) for root in binding.prefix_roots):
        raise SecurityError("source_prefix_not_allowed", "the trigger names a folder outside the allowed roots")
    return bucket, prefix


def _unsafe(rel: str) -> bool:
    return (
        rel == ""
        or rel.startswith("/")
        or "\\" in rel
        or "//" in rel
        or any(ord(c) < 32 for c in rel)
        or any(part in (".", "..") for part in rel.split("/"))
    )


@op("s3.list")
def s3_list(ctx: Any) -> list[Any]:
    p = ctx.params
    bucket, prefix = _source_location(ctx)
    s3 = ctx.services.aws.client("s3")
    objects: list[ObjectEntry] = []
    total = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            key = str(o["Key"])
            size = int(o["Size"])
            if key.endswith("/") and size == 0:
                continue  # folder marker
            rel = key[len(prefix) :]
            if _unsafe(rel):
                raise DataError("object_key_unsafe", "a source object name contains traversal or control characters")
            if not p.recursive and "/" in rel:
                continue
            if p.include and not any(glob_match(g, rel) for g in p.include):
                continue
            if any(glob_match(g, rel) for g in p.exclude):
                continue
            objects.append(ObjectEntry(key=key, rel_path=rel, size=size, etag=str(o["ETag"]).strip('"')))
            total += size
            if len(objects) > p.max_objects or (p.max_total_bytes is not None and total > p.max_total_bytes):
                raise DataError("listing_limit_exceeded", "the source folder is larger than the rule allows")
    objects.sort(key=lambda o: o.rel_path)
    listing = ObjectListing(bucket=bucket, prefix=prefix, objects=objects, count=len(objects), total_bytes=total)
    ctx.log("source_listed", objects=listing.count, bytes=listing.total_bytes)
    return [listing]
