"""destinations.yaml: the per-environment destination allow-list, owned by governance (ADR-0024).

The portal may only *choose* among these entries; it can never define a new destination. Credentials, KMS keys, IAM
resources and egress rules all come from the matched entry.
"""

from __future__ import annotations

from typing import Annotated, Literal

from factory_core.pathmatch import PatternError, parse_path_pattern
from pydantic import Field, field_validator, model_validator

from ..common import StrictModel, ba
from ..fieldtypes import (
    Attestation,
    EnvName,
    HostKeyFingerprint,
    Hostname,
    Identifier,
    KmsAlias,
    S3Bucket,
    SortedIds,
    coded_error,
)
from .security import Classification


def _pattern(v: str, *, absolute: bool) -> str:
    try:
        parse_path_pattern(v, absolute=absolute)
    except PatternError as exc:
        raise coded_error(exc.code, str(exc)) from None
    return v


class _Entry(StrictModel):
    id: Identifier = ba("Entry id", "Stable id of this allowed destination")
    slots: SortedIds = ba("Slots", "Destination slots that may use this entry", min_length=1)
    max_classification: Classification = ba("Highest classification", "Most sensitive data allowed here")


class S3Entry(_Entry):
    type: Literal["s3"]
    bucket: S3Bucket = ba("Bucket", "Bucket name")
    prefix_pattern: str = ba("Prefix pattern", "e.g. studies/{study_id}/**")
    placement: Identifier = ba("Placement", "Account alias owning the bucket", default="team")
    kms_key: KmsAlias = ba("KMS key", "KMS key alias used to encrypt delivered objects")

    @field_validator("prefix_pattern")
    @classmethod
    def _p(cls, v: str) -> str:
        return _pattern(v, absolute=False)


class SftpEntry(_Entry):
    type: Literal["sftp"]
    host: Hostname = ba("Host", "Server DNS name")
    port: int = ba("Port", "", default=22, ge=1, le=65535)
    path_pattern: str = ba("Path pattern", "e.g. /inbound/acme/{study_id}/**")
    username: str = ba("User name", "", pattern=r"^[A-Za-z0-9._-]{1,64}$")
    private_key_secret: Identifier = ba("Private key secret", "Secret name holding the SSH private key")
    host_key_algorithm: Literal["ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-256", "rsa-sha2-512"] = ba(
        "Host key type", "Type of the pinned host key; only this type is negotiated"
    )
    host_key_fingerprint: HostKeyFingerprint = ba("Pinned host key", "SHA256 fingerprint; a change needs a reviewed PR")
    at_rest_attestation: Attestation | None = ba(
        "At-rest attestation", "Evidence id that the server encrypts at rest", default=None
    )

    @field_validator("path_pattern")
    @classmethod
    def _p(cls, v: str) -> str:
        return _pattern(v, absolute=True)


class BearerAuth(StrictModel):
    type: Literal["bearer"]
    token_secret: Identifier = ba("Token secret", "Secret name holding the token")


class HttpsMtlsAuth(StrictModel):
    type: Literal["mtls"]
    cert: Identifier = ba("Client certificate secret", "")
    key: Identifier = ba("Client key secret", "")


class HttpsEntry(_Entry):
    type: Literal["https"]
    host: Hostname = ba("Host", "Server DNS name")
    port: int = ba("Port", "", default=443, ge=1, le=65535)
    path_pattern: str = ba("Path pattern", "e.g. /api/v2/studies/{study_id}/packages/*")
    method: Literal["PUT", "POST"] = ba("Method", "Upload method", default="PUT")
    auth: Annotated[BearerAuth | HttpsMtlsAuth, Field(discriminator="type")] = ba("Authentication", "")
    write_once: Literal["if_none_match", "none"] = ba(
        "Write once", "Use If-None-Match to never overwrite", default="if_none_match"
    )
    at_rest_attestation: Attestation | None = ba(
        "At-rest attestation", "Evidence id that the server encrypts at rest", default=None
    )

    @field_validator("path_pattern")
    @classmethod
    def _p(cls, v: str) -> str:
        return _pattern(v, absolute=True)

    @model_validator(mode="after")
    def _write_once(self) -> HttpsEntry:
        if self.method == "POST" and self.write_once == "if_none_match":
            raise coded_error(
                "destination_write_once_unsupported",
                "If-None-Match write-once needs PUT to the object URL; use method PUT or write_once: none",
            )
        return self


AllowedDestination = Annotated[S3Entry | SftpEntry | HttpsEntry, Field(discriminator="type")]


class DestinationsFile(StrictModel):
    """destinations.yaml: owned by governance."""

    destinations: dict[EnvName, list[AllowedDestination]] = ba(
        "Allowed destinations", "Per environment: where data may be delivered"
    )
