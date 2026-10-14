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
    stores: TeamStores | None = ba("Stores", "Run-state, staging, bundle and audit stores (mwaa layout)", default=None)
    kms_aliases: dict[Identifier, KmsAlias] = ba("KMS aliases", "Team keys by purpose", default_factory=dict)


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


class DataopsVaultSettings(StrictModel):
    """Vault behind the DataOps platform: a project token in Secrets Manager, secrets in KV v2 (ADR-0026)."""

    address: str = ba("Address", "", pattern=r"^https://[a-z0-9.-]+(:\d{1,5})?$")
    service_env: str = ba("Service environment", "Vault service environment of the token", default="prd",
                          pattern=r"^[a-z0-9-]{1,32}$")  # fmt: skip
    token_secret: str = ba(
        "Token secret",
        "Secrets Manager id of the project's Vault token; {service_env} and {entity} are filled in",
        default="/{service_env}/pcp/{entity}/vault-token",
        pattern=r"^[A-Za-z0-9/_+=.@{}-]{1,256}$",
    )
    token_region: AwsRegion = ba("Token region", "Region of the token secret", default="eu-west-1")
    kv_path: str = ba(
        "KV path",
        "Path of a secret under v1/; {entity_path} (the entity with '/' for '-') and {path} are filled in",
        default="secret/data/{entity_path}/base/{path}",
        pattern=r"^[A-Za-z0-9/_{}.-]{1,256}$",
    )


class DataopsSettings(StrictModel):
    """Per-environment settings of the dataops layout: where run state lives, how secrets are read."""

    state_bucket: S3Bucket = ba("State bucket", "Run state, staging, audit and bundles (under the state prefix)")
    state_prefix: str = ba("State prefix", "", default="_factory/", pattern=r"^[A-Za-z0-9_./-]{1,64}/$")
    vault: DataopsVaultSettings | None = ba("Vault", "For secrets read from the DataOps Vault", default=None)
    project_entity: str | None = ba(
        "Project entity",
        "Vault project entity; default: segments 2-5 of the AWS_ROLE_ARN the container receives",
        default=None,
        pattern=r"^[a-z0-9][a-z0-9-]{1,127}$",
    )


class PlatformSettings(StrictModel):
    settings_version: Literal[1]
    environment: EnvName
    region: AwsRegion
    account_model: Literal["account_per_team", "shared_per_env"]
    identity: IdentitySettings = ba("Workload identity", "", default_factory=IdentitySettings)
    accounts: dict[Identifier, Account]
    placements: dict[Identifier, PlacementDef]
    mwaa: MwaaSettings | None = ba("MWAA", "The mwaa layout's Airflow environment", default=None)
    dataops: DataopsSettings | None = ba("DataOps", "The dataops layout's settings", default=None)
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
        if self.dataops is None:
            if self.mwaa is None:
                raise ValueError("give mwaa settings (mwaa layout) or dataops settings (dataops layout)")
            for team, t in self.teams.items():
                if t.stores is None or not t.kms_aliases:
                    raise ValueError(f"team '{team}' needs stores and kms_aliases (mwaa layout)")
        return self

    @property
    def mwaa_settings(self) -> MwaaSettings:
        if self.mwaa is None:
            raise ValueError(f"the {self.environment} platform settings have no mwaa section")
        return self.mwaa

    def team_stores(self, team: str) -> TeamStores:
        stores = self.teams[team].stores
        if stores is None:
            raise ValueError(f"team '{team}' has no stores in the {self.environment} platform settings")
        return stores
