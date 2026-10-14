"""Platform settings (platform repo, one file per environment): accounts, placements, naming patterns, stores, limits.

Specs refer to placement aliases only; account ids live here. Not part of a pipeline's lock.
"""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from .common import StrictModel, ba
from .fieldtypes import AwsRegion, ByteSize, Duration, EnvName, Identifier, KmsAlias, ResourceName, S3Bucket


class Account(StrictModel):
    account_id: str = ba("Account id", "12-digit AWS account id", pattern=r"^\d{12}$")


class PlacementDef(StrictModel):
    account: str = ba(
        "Account", "Account key, or @team for the pipeline's own team account", pattern=r"^(@team|[a-z][a-z0-9_]*)$"
    )
    role: Literal["orchestration", "workload", "destination"] = ba("Role", "What runs or lives there")


class MwaaSettings(StrictModel):
    environment_name: ResourceName = ba("MWAA environment", "")
    airflow_version: str = ba("Airflow version", "", pattern=r"^\d+\.\d+\.\d+$")
    rbac_role_pattern: str = ba("RBAC role pattern", "e.g. team_{team}")
    trigger_role_pattern: str = ba("Trigger role pattern", "e.g. factory-trigger-{team}")
    launcher_role_pattern: str = ba("Launcher role pattern", "e.g. factory-launcher-{team}")


class TeamStores(StrictModel):
    staging_bucket: S3Bucket
    run_state_table: ResourceName
    bundle_store: S3Bucket
    audit_store: S3Bucket


class TeamEks(StrictModel):
    cluster: ResourceName
    namespace: str = ba("Namespace", "", pattern=r"^[a-z0-9-]{1,63}$")


class TeamSettings(StrictModel):
    account: Identifier
    eks: TeamEks | None = ba("EKS", "Only when the team has pipelines that need pods", default=None)
    stores: TeamStores
    kms_aliases: dict[Identifier, KmsAlias]


class LambdaLimitsSettings(StrictModel):
    timeout: Duration = ba("Timeout", "Lambda maximum", default="15m")
    memory: ByteSize = ba("Memory", "Lambda maximum", default="10240MiB")
    ephemeral_storage: ByteSize = ba("Ephemeral storage", "Lambda /tmp maximum", default="10240MiB")


class Limits(StrictModel):
    max_package_bytes: ByteSize = ba("Largest package", "Per run", default="10GB")
    lambda_: LambdaLimitsSettings = ba("Lambda limits", "", default_factory=LambdaLimitsSettings, alias="lambda")


class IdentitySettings(StrictModel):
    default: Literal["pod_identity", "irsa"] = "pod_identity"


class VaultSettings(StrictModel):
    address: str = ba("Address", "", pattern=r"^https://[a-z0-9.-]+(:\d{1,5})?$")
    auth_method: Literal["aws"] = ba(
        "Auth method", "AWS IAM auth with the workload role (AppRole is deferred)", default="aws"
    )
    auth_mount: str = ba("Auth mount", "", default="aws", pattern=r"^[a-z0-9_-]{1,64}$")
    role_pattern: str = ba(
        "Vault role",
        "Vault AWS-auth role per pipeline, bound to its workload role ARN",
        default="factory-{team}-{pipeline_id}",
        pattern=r"^[a-z0-9_{}-]{1,128}$",
    )
    iam_server_id: str | None = ba(
        "IAM server id",
        "Value of X-Vault-AWS-IAM-Server-ID, bound in the Vault auth config (replay protection)",
        default=None,
        pattern=r"^[a-z0-9.-]{1,253}$",
    )


class PlatformSettings(StrictModel):
    settings_version: Literal[1]
    environment: EnvName
    region: AwsRegion
    account_model: Literal["account_per_team", "shared_per_env"]
    identity: IdentitySettings = ba("Workload identity", "", default_factory=IdentitySettings)
    accounts: dict[Identifier, Account]
    placements: dict[Identifier, PlacementDef]
    mwaa: MwaaSettings
    teams: dict[Identifier, TeamSettings]
    limits: Limits = ba("Limits", "", default_factory=Limits)
    vault: VaultSettings | None = None
    allow_local_secrets: bool = ba(
        "Local secrets", "Pipelines may be run locally with secrets from the developer's file (dev only)", default=False
    )

    model_config = StrictModel.model_config | {"populate_by_name": True}

    @model_validator(mode="after")
    def _refs(self) -> PlatformSettings:
        for name, p in self.placements.items():
            if p.account != "@team" and p.account not in self.accounts:
                raise ValueError(f"placement '{name}' refers to unknown account '{p.account}'")
        for team, t in self.teams.items():
            if t.account not in self.accounts:
                raise ValueError(f"team '{team}' refers to unknown account '{t.account}'")
        return self
