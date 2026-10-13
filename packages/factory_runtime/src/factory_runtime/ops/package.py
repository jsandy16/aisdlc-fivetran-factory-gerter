"""package.zip: zip the incoming file stream into a staged, write-once package (ADR-0013).

The zip streams straight into an S3 multipart upload in staging; nothing is held on disk. Its hash record
(<artifact>.meta.json: size, MD5, SHA-256, every member's SHA-256) is written before the zip is completed, so a retry
can tell an identical package from a conflicting one without re-reading 10 GB.
"""

from __future__ import annotations

import contextlib
from typing import Any

from factory_core.errors import DataError
from factory_spec.artifact_models import PackageInfo

from ..artifacts import package_meta_name, package_name
from ..package.zipstream import ZLIB_VERSION, DeterministicZip, HashingSink, ZipOptions
from ..transports.s3 import AlreadyExists, MultipartWriter, part_size_for
from . import op


@op("package.zip")
def package_zip(ctx: Any) -> list[Any]:
    p = ctx.params
    files = ctx.input_of("file_set")
    out = ctx.output_name(0)
    staging = ctx.staging
    max_bytes = ctx.spec.pipeline.workload.bytes_per_run.max
    part = int(ctx.services.overrides.extra.get("part_size", 0)) if ctx.services.overrides else 0
    writer = MultipartWriter(
        staging.s3,
        staging.bucket,
        staging.key(package_name(out)),
        part_size=part or part_size_for(max_bytes),
        concurrency=2,
        extra=staging.sse(),
        content_type="application/zip",
    )
    sink = HashingSink(writer)
    opts = ZipOptions(p.compression, p.level, p.store_if_compressed, p.zip64)
    z = DeterministicZip(sink, opts)
    try:
        for f in files:
            with f.open() as stream:
                z.add(f.rel_path, f.size, stream)
        if not z.members:
            raise DataError("package_empty", "there are no files to package")
        z.close()
        info = PackageInfo(
            bucket=staging.bucket,
            key=staging.key(package_name(out)),
            size=sink.size,
            md5=sink.md5.hexdigest(),
            sha256=sink.sha256.hexdigest(),
            members=z.members,
            zip64=z.used_zip64,
            zlib_version=ZLIB_VERSION,
        )
        staging.put_doc(package_meta_name(out), info)  # compare-or-fail on a retry
        # AlreadyExists: an earlier attempt completed it; the equal hash record above proves it is the same package
        with contextlib.suppress(AlreadyExists):
            writer.complete()
    except BaseException:
        z.abandon()
        writer.abort()
        raise
    finally:
        writer.close()
    ctx.staged.add(out)
    ctx.services.log.metric("FilesPackaged", len(info.members), "Count", pipeline_id=ctx.run.pipeline_id)
    ctx.log("package_written", files=len(info.members), bytes=info.size, sha256=info.sha256)
    return [info]
