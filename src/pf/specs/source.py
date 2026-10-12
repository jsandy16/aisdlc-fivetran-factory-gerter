"""source.yaml — where data comes from and how a delivery (package) is recognised and checked."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .common import Regex, SecretRef, StrictModel
from .rules import PackageCheck, PackageTransform


class Readiness(StrictModel):
    """A package is processed only when it is complete."""

    min_age_seconds: int = Field(default=300, ge=0, description="Skip files modified more recently than this")
    marker: str | None = Field(default=None, description="Optional done-marker file, e.g. '{name}.done'")


class PackageSpec(StrictModel):
    """How a delivery is recognised, ordered and checked before any row is emitted."""

    match: Regex = Field(description="Regex on the path relative to root; named groups become package attributes")
    order_by: list[str] = Field(default_factory=list, description="Named groups that order packages (default: path)")
    readiness: Readiness = Field(default_factory=Readiness)
    on_unmatched: Literal["warn", "ignore"] = Field(
        default="warn", description="files matching no delivery pattern: warn = audited once in pf_package_audit + Fivetran warning"
    )
    on_redelivery: Literal["skip", "reprocess", "reject"] = Field(
        default="reject", description="Same package path seen again with different content"
    )
    max_package_bytes: int = Field(default=2 * 1024**3, ge=1)
    transforms: list[PackageTransform] = Field(default_factory=list)
    checks: list[PackageCheck] = Field(default_factory=list)

    @model_validator(mode="after")
    def _order_groups_exist(self) -> "PackageSpec":
        import re

        groups = set(re.compile(self.match).groupindex)
        missing = [g for g in self.order_by if g not in groups]
        if missing:
            raise ValueError(f"order_by names {missing} are not named groups of match")
        return self


class _FileSource(StrictModel):
    host: str = Field(min_length=1)
    username: str = Field(min_length=1)
    root: str = Field(default="/", description="Folder to scan, recursively")
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    package: PackageSpec


class SftpSource(_FileSource):
    type: Literal["sftp"]
    port: int = Field(default=22, ge=1, le=65535)
    private_key: SecretRef | None = Field(default=None, description="PEM private key (preferred over password)")
    password: SecretRef | None = None
    host_key: str = Field(
        min_length=20,
        description="Pinned server public key, e.g. 'ssh-ed25519 AAAA...'. Never defaulted: connecting to an unpinned host is refused.",
    )

    @model_validator(mode="after")
    def _one_auth(self) -> "SftpSource":
        if (self.private_key is None) == (self.password is None):
            raise ValueError("set exactly one of private_key or password")
        return self


class FtpSource(_FileSource):
    type: Literal["ftp"]
    port: int = Field(default=21, ge=1, le=65535)
    password: SecretRef
    tls: Literal["explicit", "none"] = Field(
        default="explicit", description="explicit = FTPS (AUTH TLS) with an encrypted data channel; none needs an exception"
    )
    passive: bool = True
    ca_cert: SecretRef | None = Field(default=None, description="PEM CA bundle to trust instead of the system store (private CA)")


class RdsSource(StrictModel):
    type: Literal["rds"]
    engine: Literal["postgres", "mysql"]
    host: str = Field(min_length=1)
    port: int | None = Field(default=None, ge=1, le=65535, description="Defaults: postgres 5432, mysql 3306")
    database: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: SecretRef
    ssl_mode: Literal["verify-full", "require", "disable"] = "verify-full"
    ca_cert: SecretRef | None = Field(default=None, description="PEM CA bundle, e.g. the AWS RDS global bundle (needed for verify-full)")
    batch_size: int = Field(default=5000, ge=100, le=100000)
    timeout_seconds: int = Field(default=60, ge=5, le=600)


Source = Annotated[Union[SftpSource, FtpSource, RdsSource], Field(discriminator="type")]


class SourceFile(StrictModel):
    """source.yaml"""

    source: Source


FILE_SOURCES = {"sftp", "ftp"}
