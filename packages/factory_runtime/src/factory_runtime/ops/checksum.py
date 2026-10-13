"""checksum.compute: re-read the stored package and recompute its checksums (an independent check, ADR-0013).

The S3 ETag is never used as a checksum (it is not an MD5 for multipart or SSE-KMS objects). A mismatch with the hash
record written while zipping, or with the manifest, stops the run (data, 65).
"""

from __future__ import annotations

import hashlib
from typing import Any

from factory_core.errors import DataError
from factory_spec.artifact_models import ChecksumRecord, Manifest, PackageInfo

from ..transports.s3 import iter_object
from . import op


@op("checksum.compute")
def checksum_compute(ctx: Any) -> list[Any]:
    p = ctx.params
    pkg: PackageInfo = ctx.input_of("package")
    manifest: Manifest | None = ctx.input_of("manifest", required=False)
    hashes = {a: hashlib.new(a, usedforsecurity=False) if a == "md5" else hashlib.new(a) for a in p.algorithms}
    size = 0
    for chunk in iter_object(ctx.services.aws.client("s3"), pkg.bucket, pkg.key):
        size += len(chunk)
        for h in hashes.values():
            h.update(chunk)
    digests = {a: h.hexdigest() for a, h in hashes.items()}
    recorded = {"md5": pkg.md5, "sha256": pkg.sha256}
    if size != pkg.size or any(a in recorded and digests[a] != recorded[a] for a in digests):
        raise DataError("checksum_mismatch", "the stored package differs from the package that was written")
    compared = False
    if p.compare_with_manifest and manifest is not None:
        expected = {"md5": manifest.package.md5, "sha256": manifest.package.sha256}
        for a, v in expected.items():
            if v is not None and a in digests and digests[a] != v:
                raise DataError("checksum_mismatch", "the stored package differs from its manifest")
        compared = True
    return [ChecksumRecord(size=size, digests=digests, compared_with_manifest=compared)]
