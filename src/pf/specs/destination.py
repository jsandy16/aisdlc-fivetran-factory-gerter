"""destination.yaml — the Fivetran Managed Data Lake destination on Amazon S3, per environment.

Field names map one to one onto Fivetran's REST API for service `managed_data_lake`
(POST /v1/destinations, config.storage_provider = AWS).
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from .common import Environment, IamRoleArn, StrictModel

BucketName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")]
AwsRegion = Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(-gov)?-[a-z]+-\d$")]


class S3Environment(StrictModel):
    fivetran_group: str = Field(min_length=1, description="Fivetran destination (group) name, unique per environment")
    bucket: BucketName = Field(description="Immutable after the destination is created")
    prefix_path: str = Field(default="fivetran", description="Immutable; must not start or end with '/'")
    region: AwsRegion = Field(description="AWS region of the bucket, e.g. us-east-1 (immutable)")
    fivetran_role_arn: IamRoleArn = Field(description="Role Fivetran assumes (trusts Fivetran's account + your External ID)")
    glue_catalog: bool = Field(default=True, description="Register tables in AWS Glue; can only be chosen at creation")
    snapshot_retention_period: Literal[
        "ONE_DAY", "ONE_WEEK", "TWO_WEEKS", "FOUR_WEEKS", "SIX_WEEKS", "RETAIN_ALL_SNAPSHOTS"
    ] = "ONE_WEEK"
    write_delta: bool = False

    @field_validator("prefix_path")
    @classmethod
    def _no_slashes(cls, v: str) -> str:
        if v.startswith("/") or v.endswith("/"):
            raise ValueError("prefix_path must not start or end with '/'")
        return v


class S3Destination(StrictModel):
    type: Literal["s3"]
    processing_region: str = Field(
        pattern=r"^[A-Z0-9_]+$", description="Fivetran data processing location, e.g. AWS_US_EAST_1"
    )
    time_zone_offset: str = Field(default="0", pattern=r"^[+-]?\d{1,2}$")
    environments: dict[Environment, S3Environment] = Field(min_length=1)


class DestinationFile(StrictModel):
    """destination.yaml"""

    destination: S3Destination
