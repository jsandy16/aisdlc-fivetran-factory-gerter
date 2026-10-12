"""policy.yaml — governance decisions: classification, access, retention, audit, original files.

Owned by governance. Enforced outside Fivetran: the build generates Lake Formation tags/grants,
an audit bucket with S3 Object Lock and CloudTrail data events; originals are archived write-once.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import Environment, IamRoleArn, SecretRef, StrictModel
from .destination import BucketName

Classification = Literal["public", "internal", "confidential", "restricted"]


class AccessGrant(StrictModel):
    principal: IamRoleArn
    classifications: list[Classification] = Field(min_length=1, description="Data this role may read")
    permissions: list[Literal["SELECT", "DESCRIBE"]] = Field(default_factory=lambda: ["SELECT", "DESCRIBE"])
    justification: str = Field(min_length=5)


class AuditEnvironment(StrictModel):
    bucket: BucketName = Field(description="Write-once evidence bucket (S3 Object Lock)")
    object_lock_mode: Literal["GOVERNANCE", "COMPLIANCE"] = "COMPLIANCE"
    cloudtrail_data_events: bool = True


class OriginalsEnvironment(StrictModel):
    bucket: BucketName = Field(description="Write-once bucket for original files exactly as received")
    prefix: str = Field(default="originals")
    region: str = Field(pattern=r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
    access_key_id: SecretRef
    secret_access_key: SecretRef


class EnvironmentPolicy(StrictModel):
    access: list[AccessGrant] = Field(default_factory=list)
    audit: AuditEnvironment
    originals: OriginalsEnvironment | None = None


class Retention(StrictModel):
    days: int = Field(ge=1)
    rationale: str = Field(min_length=10, description="Why this long — never a default")
    object_lock_days: int = Field(ge=1, description="Object Lock retention for originals and evidence")


class PolicyFile(StrictModel):
    """policy.yaml"""

    default_classification: Classification = Field(description="Applied to columns not listed in classification")
    classification: dict[str, Classification] = Field(
        default_factory=dict, description="entity.attribute -> classification"
    )
    archive_originals: bool = Field(default=True, description="Keep every received file byte-for-byte (file sources)")
    retention: Retention
    environments: dict[Environment, EnvironmentPolicy] = Field(min_length=1)

    @model_validator(mode="after")
    def _keys_form(self) -> "PolicyFile":
        bad = [k for k in self.classification if k.count(".") != 1]
        if bad:
            raise ValueError(f"classification keys must be entity.attribute: {bad}")
        return self
