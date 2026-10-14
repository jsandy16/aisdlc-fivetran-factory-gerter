"""environments.yaml: per-environment bindings, owned by the platform team (part of binding_sha, not logic_sha)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ..common import StrictModel, ba
from ..fieldtypes import EnvName, HttpsBaseUrl, Identifier, PrefixRoot, ResourceName, S3Bucket

PlacementAlias = Identifier


class Placement(StrictModel):
    orchestration: PlacementAlias = ba(
        "Orchestration placement", "Placement alias of the MWAA environment", default="mwaa"
    )
    workload: PlacementAlias = ba("Workload placement", "Placement alias of the team account", default="team")


class VaultBinding(StrictModel):
    mount: str = ba("KV mount", "Vault KV v2 mount", pattern=r"^[a-z0-9_-]{1,64}$")
    namespace: str | None = ba("Namespace", "Vault Enterprise namespace", default=None, pattern=r"^[a-z0-9_/-]{1,128}$")


class SecretsManagerBinding(StrictModel):
    prefix: str = ba(
        "Name prefix", "Prefix of secret names in this account", default="", pattern=r"^[A-Za-z0-9/_+=.@-]{0,128}$"
    )


SecretStore = Literal["vault", "aws_secrets_manager"]


class SecretStores(StrictModel):
    default: SecretStore | None = ba(
        "Default store", "Where this environment reads secrets (local runs use the developer's file)", default=None
    )
    overrides: dict[Identifier, SecretStore] = ba(
        "Per-secret store", "Secrets read from another store in this environment", default_factory=dict
    )
    vault: VaultBinding | None = ba("Vault", "Vault binding", default=None)
    aws_secrets_manager: SecretsManagerBinding | None = ba("Secrets Manager", "Secrets Manager binding", default=None)


class QueueBinding(StrictModel):
    name: ResourceName = ba("Queue name", "SQS queue name in the team account")


class SourceBinding(StrictModel):
    buckets: list[S3Bucket] = ba(
        "Allowed buckets", "Buckets this source may read in this environment", default_factory=list
    )
    prefix_roots: list[PrefixRoot] = ba(
        "Allowed prefixes", "Folders a trigger-supplied prefix must start with", default_factory=list
    )
    placement: PlacementAlias = ba("Placement", "Account alias of the buckets", default="team")
    bucket: S3Bucket | None = ba("Bucket", "For static sources: the bucket", default=None)
    fivetran_group: str | None = ba(
        "Fivetran group",
        "For database sources: the Fivetran destination group id",
        default=None,
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
    )


class ApiBinding(StrictModel):
    base_url: HttpsBaseUrl = ba("Base URL", "https:// base URL in this environment")


class SnsChannel(StrictModel):
    type: Literal["sns"]
    topic: ResourceName = ba("SNS topic", "Topic name in the team account")


class EmailChannel(StrictModel):
    type: Literal["email"]
    address: str = ba("Email", "Distribution list", pattern=r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")


class SlackChannel(StrictModel):
    type: Literal["slack"]
    channel: str = ba("Slack channel", "e.g. #clinops-alerts", pattern=r"^#[a-z0-9_-]{1,80}$")


AlertChannel = Annotated[SnsChannel | EmailChannel | SlackChannel, Field(discriminator="type")]


class EnvBinding(StrictModel):
    enabled: bool = ba("Enabled", "Deploy to this environment", default=True)
    placement: Placement = ba("Placement", "Account aliases", default_factory=Placement)
    secrets: SecretStores = ba("Secret stores", "Secret store bindings", default_factory=SecretStores)
    queues: dict[Identifier, QueueBinding] = ba("Queues", "Queue names by queue id", default_factory=dict)
    sources: dict[Identifier, SourceBinding] = ba("Sources", "Source bindings by source id", default_factory=dict)
    portal: ApiBinding | None = ba("Portal", "Portal base URL", default=None)
    apis: dict[Identifier, ApiBinding] = ba("APIs", "API base URLs by API id", default_factory=dict)
    alert_channels: dict[Identifier, AlertChannel] = ba("Alert channels", "Channels by name", default_factory=dict)
    schedule_cron: str | None = ba("Schedule override", "Cron override for this environment", default=None)


class EnvironmentsFile(StrictModel):
    """environments.yaml: owned by the platform team."""

    environments: dict[EnvName, EnvBinding] = ba("Environments", "Bindings per environment (dev, test, ppt, prod)")
