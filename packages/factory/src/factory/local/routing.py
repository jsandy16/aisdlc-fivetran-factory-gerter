"""AWS clients for local runs: the framework's own stores on a local endpoint, the pipeline's data in the dev account.

S3 calls go to the local endpoint when their bucket is one of the framework's stores (staging, bundles, audit), and to
AWS otherwise; DynamoDB (run state) is local; everything else (SQS, Secrets Manager, STS) is AWS. The one call that
spans both sides, a server-side multipart copy from staging to a real destination bucket, becomes a ranged read from
the local store plus an upload part, with the same write-once completion as in production.
"""

from __future__ import annotations

from typing import Any

from factory_runtime.aws import _BOTO_CONFIG, AwsClients
from factory_runtime.config import Overrides, RuntimeConfig


class _RoutedPaginator:
    def __init__(self, router: RoutedS3, name: str) -> None:
        self.router, self.name = router, name

    def paginate(self, **kw: Any) -> Any:
        return self.router.pick(kw.get("Bucket")).get_paginator(self.name).paginate(**kw)


class RoutedS3:
    def __init__(self, local: Any, remote: Any, local_buckets: frozenset[str]) -> None:
        self.local, self.remote, self.local_buckets = local, remote, local_buckets

    def pick(self, bucket: str | None) -> Any:
        return self.local if bucket in self.local_buckets else self.remote

    def get_paginator(self, name: str) -> _RoutedPaginator:
        return _RoutedPaginator(self, name)

    def upload_part_copy(self, **kw: Any) -> Any:
        src = kw["CopySource"]["Bucket"]
        dst = self.pick(kw["Bucket"])
        if self.pick(src) is dst:
            return dst.upload_part_copy(**kw)
        get = {"Bucket": src, "Key": kw["CopySource"]["Key"], "Range": kw.get("CopySourceRange")}
        if kw.get("CopySourceIfMatch"):
            get["IfMatch"] = kw["CopySourceIfMatch"]
        body = self.pick(src).get_object(**{k: v for k, v in get.items() if v})["Body"].read()
        put = {k: kw[k] for k in ("Bucket", "Key", "UploadId", "PartNumber", "ExpectedBucketOwner") if k in kw}
        etag = dst.upload_part(Body=body, **put)["ETag"]
        return {"CopyPartResult": {"ETag": etag}}

    def __getattr__(self, name: str) -> Any:
        def call(**kw: Any) -> Any:
            return getattr(self.pick(kw.get("Bucket")), name)(**kw)

        return call


class RoutedAwsClients(AwsClients):
    def __init__(self, config: RuntimeConfig, overrides: Overrides | None, *, local_endpoint: str,
                 remote_endpoint: str | None = None) -> None:  # fmt: skip
        super().__init__(config, None)
        self.endpoint_url = remote_endpoint  # tests only: a second local endpoint stands in for the dev account
        self.local_endpoint = local_endpoint
        self.local_buckets = frozenset({config.staging_bucket, config.bundle_store, config.audit_store})
        self._local: dict[str, Any] = {}

    def _local_client(self, service: str) -> Any:
        if service not in self._local:
            self._local[service] = self.session.client(service, endpoint_url=self.local_endpoint, config=_BOTO_CONFIG)
        return self._local[service]

    def client(self, service: str) -> Any:
        if service == "dynamodb":
            return self._local_client("dynamodb")
        if service == "s3":
            with self._lock:
                routed = self._clients.get("s3-routed")
            if routed is None:
                routed = RoutedS3(self._local_client("s3"), super().client("s3"), self.local_buckets)
                with self._lock:
                    self._clients["s3-routed"] = routed
            return routed
        return super().client(service)
