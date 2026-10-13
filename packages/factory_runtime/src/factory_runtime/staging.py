"""Staged artifacts: s3://<staging bucket>/<pipeline_id>/<run_dir>/<name>, write-once, SSE-KMS (team key).

Every write uses If-None-Match: *. When the object already exists (a retry), the stored bytes are compared:
- equal: the retry produced the same artifact, nothing to do;
- snapshot kinds (the trigger message, the portal answer, the source listing): the stored copy is the run's custody
  record and is adopted, so every retry and later task sees the same answer;
- anything else: a data error (the inputs changed under a running run).
`rev-<n>/` below the run folder is reserved for forced reruns (later minor version).
"""

from __future__ import annotations

from typing import Any, TypeVar

from factory_core.errors import ConfigError, DataError

from .aws import AwsClients

D = TypeVar("D")


class Staging:
    def __init__(self, aws: AwsClients, bucket: str, kms_key: str | None, pipeline_id: str, run_dir: str) -> None:
        self.aws = aws
        self.bucket = bucket
        self.kms_key = kms_key
        self.prefix = f"{pipeline_id}/{run_dir}/"

    @property
    def s3(self) -> Any:
        return self.aws.client("s3")

    def key(self, name: str) -> str:
        return self.prefix + name

    def sse(self) -> dict[str, str]:
        if self.kms_key:
            return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.kms_key}
        return {"ServerSideEncryption": "aws:kms"}

    def put_once(self, name: str, data: bytes, *, content_type: str = "application/json") -> bool:
        """True when written now, False when identical bytes were already there; raises on different bytes."""
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=self.key(name),
                Body=data,
                ContentType=content_type,
                IfNoneMatch="*",
                **self.sse(),
            )
            return True
        except Exception as exc:
            if not self.aws.precondition_failed(exc):
                raise
        if self.get_bytes(name) == data:
            return False
        raise DataError("staged_artifact_conflict", f"staged artifact '{name}' already exists with other content")

    def put_doc(self, name: str, doc: D, *, adopt: bool = False) -> D:
        """Write a canonical JSON document once; on a retry compare it, or adopt the stored one (snapshots)."""
        data: bytes = doc.canonical_json()  # type: ignore[attr-defined]
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=self.key(name),
                Body=data,
                ContentType="application/json",
                IfNoneMatch="*",
                **self.sse(),
            )
            return doc
        except Exception as exc:
            if not self.aws.precondition_failed(exc):
                raise
        stored = self.get_bytes(name)
        if stored == data:
            return doc
        if adopt:
            return type(doc).model_validate_json(stored)  # type: ignore[attr-defined, no-any-return]
        raise DataError("staged_artifact_conflict", f"staged artifact '{name}' already exists with other content")

    def get_bytes(self, name: str) -> bytes:
        try:
            res = self.s3.get_object(Bucket=self.bucket, Key=self.key(name))
        except Exception as exc:
            if self.aws.error_code(exc) in ("NoSuchKey", "404") or self.aws.status(exc) == 404:
                raise ConfigError("staged_artifact_missing", f"staged artifact '{name}' does not exist") from None
            raise
        body: bytes = res["Body"].read()
        return body

    def get_doc(self, name: str, model: type[D]) -> D:
        return model.model_validate_json(self.get_bytes(name))  # type: ignore[attr-defined, no-any-return]

    def exists(self, name: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self.key(name))
            return True
        except Exception as exc:
            if self.aws.status(exc) == 404 or self.aws.error_code(exc) in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
