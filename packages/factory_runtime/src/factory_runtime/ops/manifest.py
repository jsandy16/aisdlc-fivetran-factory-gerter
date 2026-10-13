"""manifest.build: list every file in the package with its size and checksums, plus the package's own checksums.

The manifest is cross-checked against the listing (every listed file is in the package with the same size), staged,
and copied to the audit store as lineage evidence. It names the run by its hash only.
"""

from __future__ import annotations

from typing import Any

from factory_core.errors import DataError
from factory_spec.artifact_models import Manifest, ManifestFile, ManifestPackage, ObjectListing, PackageInfo

from . import op


@op("manifest.build")
def manifest_build(ctx: Any) -> list[Any]:
    p = ctx.params
    pkg: PackageInfo = ctx.input_of("package")
    listing: ObjectListing | None = ctx.input_of("object_listing", required=False)
    if listing is not None:
        in_pkg = {(m.path, m.size) for m in pkg.members}
        expected = {(o.rel_path, o.size) for o in listing.objects} | {(o.key, o.size) for o in listing.objects}
        if not in_pkg <= expected or len(in_pkg) != listing.count:
            raise DataError("manifest_mismatch", "the package does not hold exactly the listed files")
    files = [
        ManifestFile(
            path=m.path,
            size=m.size,
            sha256=m.sha256 if "sha256" in p.file_algorithms else "",
            md5=m.md5 if "md5" in p.file_algorithms else None,
        )
        for m in pkg.members
    ]
    package = ManifestPackage(
        size=pkg.size,
        md5=pkg.md5 if "md5" in p.package_algorithms else None,
        sha256=pkg.sha256 if "sha256" in p.package_algorithms else None,
    )
    source = {"bucket": listing.bucket, "prefix": listing.prefix} if listing is not None else {}
    manifest = Manifest(
        pipeline_id=ctx.run.pipeline_id,
        run=ctx.run.run_key_hash,
        source=source,
        package=package,
        files=files,
        file_count=len(files),
        total_bytes=sum(f.size for f in files),
    )
    ctx.audit.put_evidence(f"manifest-{ctx.output_name(0)}.json", manifest)
    return [manifest]
