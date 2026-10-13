"""s3.download: stream the listed objects in listing order (lazy: nothing is read unless a consumer pulls).

Each object is read with If-Match on its listed ETag (`verify: size_and_etag`), so a file replaced after listing
stops the run (data, 65) instead of silently entering the package; the byte count is checked at the end of every
object. Small objects are read ahead in parallel (at most `concurrency`, each at most READ_AHEAD_MAX bytes) to hide
request latency; large objects stream straight through, so memory stays bounded.
"""

from __future__ import annotations

import functools
import io
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from factory_spec.artifact_models import ObjectEntry, ObjectListing

from ..artifacts import FileStream
from ..transports.s3 import open_object
from . import op

READ_AHEAD_MAX = 8 * 1024 * 1024


@op("s3.download")
def s3_download(ctx: Any) -> list[Any]:
    listing: ObjectListing = ctx.input_of("object_listing")
    p = ctx.params
    s3 = ctx.services.aws.client("s3")
    full_key = False
    for r in ctx.spec.rules.rules:  # the package rule decides how paths appear in the zip
        impl = r.implementation
        if impl.kind == "builtin" and impl.op == "package.zip" and set(ctx.rule.outputs) & set(r.inputs):
            full_key = impl.params.paths == "full_key"

    def opener(o: ObjectEntry) -> Any:
        etag = o.etag if p.verify == "size_and_etag" else None
        return lambda: open_object(s3, listing.bucket, o.key, if_match=etag, size=o.size, what="a source object")

    def read_all(o: ObjectEntry) -> bytes:
        with opener(o)() as fh:
            data: bytes = fh.read()
            return data

    def files() -> Iterator[FileStream]:
        with ThreadPoolExecutor(max_workers=p.concurrency, thread_name_prefix="s3-read") as pool:
            pending: dict[int, Future[bytes]] = {}
            objs = listing.objects
            for i, o in enumerate(objs):
                # keep up to `concurrency` small objects in flight ahead of the consumer
                for j in range(i, min(len(objs), i + p.concurrency)):
                    if j not in pending and objs[j].size <= READ_AHEAD_MAX:
                        pending[j] = pool.submit(read_all, objs[j])
                name = o.key if full_key else o.rel_path
                fut = pending.pop(i, None)
                if fut is not None:
                    data = fut.result()
                    yield FileStream(name, o.size, functools.partial(io.BytesIO, data), o.key)
                else:
                    yield FileStream(name, o.size, opener(o), o.key)

    return [files()]
