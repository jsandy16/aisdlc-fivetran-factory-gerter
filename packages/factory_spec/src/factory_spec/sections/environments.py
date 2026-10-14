"""environments.yaml: per-environment bindings, owned by the platform team (part of binding_sha, not logic_sha)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue

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


SecretStore = Literal["vault", "aws_secrets_manager", "dataops_vault"]


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


InfraMode = Literal["create", "existing"]
INFRA_GROUPS = ("roles", "queues", "logs", "compute", "alerts", "resources")


class InfraGroup(StrictModel):
    mode: InfraMode = ba(
        "Owner", "create: the framework creates it; existing: the infra team provides it", default="create"
    )
    names: dict[Identifier, str] = ba(
        "Existing names", "Per resource key (task id, dispatcher, trigger, dlq): the name the infra team used",
        default_factory=dict,
    )  # fmt: skip
    adopt: bool = ba("Adopt", "create only: take over resources that already exist (Terraform import)", default=False)
    import_ids: dict[str, str] = ba(
        "Import ids", "Terraform import id per resource address, where it cannot be derived", default_factory=dict
    )


class Infrastructure(StrictModel):
    """Who owns the pipeline's AWS resources in this environment (team stores, KMS, MWAA and EKS are platform's)."""

    mode: InfraMode | None = ba("Default owner", "Applies to every group not set below", default=None)
    roles: InfraGroup = ba("IAM roles", "Task and dispatcher roles and their policies", default_factory=InfraGroup)
    queues: InfraGroup = ba("Queues", "Trigger queue, dead-letter queue, queue policy", default_factory=InfraGroup)
    logs: InfraGroup = ba("Log groups", "Lambda log groups", default_factory=InfraGroup)
    compute: InfraGroup = ba(
        "Compute", "Lambda functions, event-source mapping, Pod Identity associations", default_factory=InfraGroup
    )
    alerts: InfraGroup = ba("Alerts", "Topics, subscriptions and alarms for alert routing", default_factory=InfraGroup)
    resources: InfraGroup = ba(
        "Catalog resources", "The resources declared in environments.yaml → resources", default_factory=InfraGroup
    )

    def mode_of(self, group: str) -> str:
        g: InfraGroup = getattr(self, group)
        return g.mode if "mode" in g.model_fields_set else (self.mode or "create")


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
    infrastructure: Infrastructure = ba(
        "Infrastructure owner", "Create the pipeline's AWS resources, or use ones the infra team made",
        default_factory=Infrastructure,
    )  # fmt: skip


class ResourceInstance(StrictModel):
    """An AWS resource from the resource catalog (catalog/resources.v1.yaml), asked for by configuration."""

    type: Identifier = ba("Type", "A resource type from the resource catalog, e.g. sns_topic, cloudwatch_metric_alarm")
    name: str | None = ba(
        "Name", "AWS name; <env> becomes the environment. Default: factory-<team>-<pipeline>-<id>", default=None,
        pattern=r"^[A-Za-z0-9<>._/-]{1,255}$",
    )  # fmt: skip
    params: dict[str, JsonValue] = ba("Parameters", "As the catalog type defines them", default_factory=dict)
    environments: list[EnvName] | None = ba("Environments", "Only in these environments (default: all)", default=None)


class EnvironmentsFile(StrictModel):
    """environments.yaml: owned by the platform team."""

    environments: dict[EnvName, EnvBinding] = ba("Environments", "Bindings per environment (dev, test, ppt, prod)")
    resources: dict[Identifier, ResourceInstance] = ba(
        "Resources", "Extra AWS resources by configuration: topics, alarms, dashboards, rules, buckets, queues, tables",
        default_factory=dict,
    )  # fmt: skip
