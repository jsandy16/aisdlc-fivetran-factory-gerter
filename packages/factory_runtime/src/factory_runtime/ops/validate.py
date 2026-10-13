"""validate.* ops: checks over listings and manifests. A failed check raises CheckFailed (follows on_failure)."""

from __future__ import annotations

from typing import Any

from . import CheckFailed, op


@op("validate.file_count")
def file_count(ctx: Any) -> list[Any]:
    listing = ctx.input_of("object_listing")
    p = ctx.params
    if (p.min is not None and listing.count < p.min) or (p.max is not None and listing.count > p.max):
        raise CheckFailed("file_count_out_of_range", f"{listing.count} files; allowed {p.min}..{p.max}")
    return []


@op("validate.total_size")
def total_size(ctx: Any) -> list[Any]:
    listing = ctx.input_of("object_listing")
    p = ctx.params
    if (p.min is not None and listing.total_bytes < p.min) or (p.max is not None and listing.total_bytes > p.max):
        raise CheckFailed("total_size_out_of_range", f"{listing.total_bytes} bytes; allowed {p.min}..{p.max}")
    return []


@op("validate.manifest_match")
def manifest_match(ctx: Any) -> list[Any]:
    manifest = ctx.input_of("manifest")
    listing = ctx.input_of("object_listing")
    in_manifest = {(f.path, f.size) for f in manifest.files}
    in_listing = {(o.rel_path, o.size) for o in listing.objects}
    if in_manifest != in_listing:
        missing = len(in_listing - in_manifest)
        extra = len(in_manifest - in_listing)
        raise CheckFailed("manifest_mismatch", f"manifest differs from the listing ({missing} missing, {extra} extra)")
    return []
