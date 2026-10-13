"""pipeline.yaml: identity, variables, trigger, orchestration, reliability, observability, workload, cost."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from ..common import StrictModel, ba
from ..fieldtypes import ByteSize, Duration, Handle, Identifier, Money, RequirementId, SemVer, TemplateStr
from .fields import RecordSpec

VariableSource = Annotated[
    str,
    StringConstraints(
        pattern=r"^(trigger\.(message|conf|object|schedule)\.[a-z][a-z0-9_]*|portal\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)$"
    ),
]
_CRON = re.compile(r"^(\S+\s){4}\S+$")


class Owners(StrictModel):
    business_owner: Handle = ba("Business owner", "GitHub handle or @org/team")
    data_engineer: Handle = ba("Data engineer", "GitHub handle or @org/team")
    governance: Handle = ba("Governance", "GitHub handle or @org/team")
    platform: Handle = ba("Platform", "GitHub handle or @org/team")


class Metadata(StrictModel):
    pipeline_id: Identifier = ba("Pipeline id", "Stable id; never changes after the first deploy")
    name: str = ba("Name", "Short human name", min_length=3, max_length=120)
    description: str = ba("Description", "What the pipeline does, in plain English", min_length=10, max_length=4000)
    team: Identifier = ba("Team", "Owning team (matches the team account in platform settings)")
    spec_version: SemVer = ba("Pipeline version", "Semantic version of this pipeline")
    owners: Owners = ba("Owners", "Who approves each part of the spec")
    requirements: list[RequirementId] = ba("Requirements", "Requirement ids for traceability", default_factory=list)
    tags: dict[Identifier, str] = ba("Tags", "Cost allocation and grouping tags", default_factory=dict)


class ScheduleTrigger(StrictModel):
    type: Literal["schedule"]
    cron: str = ba("Schedule", "Five-field cron expression, in UTC, e.g. '0 6 * * *'")

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        if not _CRON.match(v.strip()):
            raise ValueError("write a five-field cron expression, e.g. '0 6 * * *'")
        return v


class SqsTrigger(StrictModel):
    type: Literal["sqs"]
    mode: Literal["dispatch", "in_task"] = ba(
        "Consumption mode", "dispatch: a Lambda snapshots the message and starts the run (default)", default="dispatch"
    )
    queue: Identifier = ba("Queue", "Queue id; its name per environment is in environments.yaml")
    max_receive_count: int = ba(
        "Delivery attempts", "Attempts before a message goes to the dead-letter queue", default=5, ge=1, le=20
    )
    envelope: Literal["raw", "sns", "eventbridge"] = ba("Message envelope", "How the message is wrapped", default="raw")
    message: RecordSpec = ba("Message fields", "The fields the message carries")


class S3EventTrigger(StrictModel):
    type: Literal["s3_event"]
    queue: Identifier = ba("Queue", "Queue receiving the S3 event notifications")
    max_receive_count: int = ba("Delivery attempts", "Attempts before the dead-letter queue", default=5, ge=1, le=20)
    key_prefix: str | None = ba("Key prefix", "Only objects under this prefix", default=None)
    key_suffix: str | None = ba("Key suffix", "Only objects ending with this", default=None)


class ManualTrigger(StrictModel):
    type: Literal["manual"]
    conf: RecordSpec = ba("Run parameters", "Fields a person supplies when starting a run (run_key is required)")


Trigger = Annotated[ScheduleTrigger | SqsTrigger | S3EventTrigger | ManualTrigger, Field(discriminator="type")]
Engine = Literal["auto", "lambda", "eks_pod", "eks_pod_spot", "mwaa", "fivetran_native", "fivetran_sdk"]


class RetryPolicy(StrictModel):
    max_attempts: int = ba("Attempts", "Total attempts for transient failures", default=3, ge=1, le=10)
    backoff: Literal["exponential_jitter"] = ba(
        "Backoff", "Wait strategy between attempts", default="exponential_jitter"
    )
    base: Duration = ba("First wait", "Wait before the second attempt", default="10s")
    max: Duration = ba("Longest wait", "Upper bound for one wait", default="5m")


class Task(StrictModel):
    task_id: Identifier = ba("Task id", "Stable id, e.g. t1_collect")
    label: str = ba("Label", "What the task does", default="", max_length=200)
    rules: list[Identifier] = ba("Rules", "Rule ids run by this task, in order", min_length=1)
    depends_on: list[Identifier] = ba("Depends on", "Tasks that must finish first", default_factory=list)
    engine: Engine = ba("Engine", "Where the task runs; auto lets the planner decide", default="auto")
    timeout: Duration = ba("Timeout", "The task fails after this long", default="30m")
    retry: RetryPolicy | None = ba("Retry policy", "Overrides the pipeline retry policy", default=None)


class DagSettings(StrictModel):
    catchup: bool = ba("Catch up", "Run missed schedule intervals", default=False)
    max_active_runs: int = ba("Parallel runs", "Runs of this pipeline at the same time", default=1, ge=1, le=64)
    timeout: Duration = ba("Run timeout", "A whole run fails after this long", default="4h")


class Orchestration(StrictModel):
    dag: DagSettings = ba("DAG settings", "Airflow DAG options", default_factory=DagSettings)
    pool: Identifier | None = ba("Pool", "Airflow pool (default: the team pool)", default=None)
    tasks: list[Task] = ba("Tasks", "Tasks of the DAG", min_length=1)


class Reliability(StrictModel):
    run_key: TemplateStr = ba("Run key", "Deterministic key of one run, from trigger fields, e.g. '{request_id}'")
    retry: RetryPolicy = ba("Retry policy", "Default retry policy for tasks", default_factory=RetryPolicy)
    replay_window: Duration = ba("Replay window", "How long failed runs can be replayed", default="30d")


AlertTarget = Identifier | Literal["none"]


class Alerts(StrictModel):
    transient: AlertTarget = ba("Transient failures", "Alert channel after retries are exhausted", default="none")
    data: AlertTarget = ba("Data failures", "Alert channel for data problems")
    config: AlertTarget = ba("Configuration failures", "Alert channel for configuration problems")
    security: AlertTarget = ba("Security failures", "Alert channel for security problems")


class Observability(StrictModel):
    alerts: Alerts = ba("Alerts", "Where each kind of failure is reported")
    sla: Duration | None = ba("SLA", "Alert when a run takes longer than this", default=None)


class Distribution(StrictModel):
    p50: ByteSize = ba("Typical", "Median size per run")
    p95: ByteSize = ba("Large", "95th percentile size per run")
    max: ByteSize = ba("Largest", "Largest size per run")

    @model_validator(mode="after")
    def _ordered(self) -> Distribution:
        if not self.p50 <= self.p95 <= self.max:
            raise ValueError("sizes must satisfy p50 <= p95 <= max")
        return self


class CountDistribution(StrictModel):
    p50: int = ba("Typical", "Median count per run", ge=0)
    p95: int = ba("Large", "95th percentile count per run", ge=0)
    max: int = ba("Largest", "Largest count per run", ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> CountDistribution:
        if not self.p50 <= self.p95 <= self.max:
            raise ValueError("counts must satisfy p50 <= p95 <= max")
        return self


class Workload(StrictModel):
    bytes_per_run: Distribution = ba("Data per run", "Bytes moved per run")
    files_per_run: CountDistribution = ba("Files per run", "Files per run")
    runs_per_day: int = ba("Runs per day", "Expected runs per day", ge=0, le=100_000)


class Cost(StrictModel):
    budget_per_run_usd: Money = ba("Budget per run", "USD per run")
    budget_per_month_usd: Money = ba("Budget per month", "USD per month")


class ArtifactOverride(StrictModel):
    durability: Literal["local", "staged"] = ba("Durability", "Raise an artifact to staged storage")


class PipelineFile(StrictModel):
    """pipeline.yaml: owned by the data engineer."""

    api_version: Literal["factory/v1"] = ba("API version", "Spec API version")
    kind: Literal["Pipeline"] = ba("Kind", "Document kind")
    schema_version: SemVer = ba("Schema version", "Version of the spec schema this file follows")
    metadata: Metadata = ba("Metadata", "Identity and ownership")
    variables: dict[Identifier, VariableSource] = ba(
        "Variables", "Named run values, e.g. study_id: trigger.message.study_id", default_factory=dict
    )
    trigger: Trigger = ba("Trigger", "What starts a run")
    orchestration: Orchestration = ba("Orchestration", "Tasks and their order")
    reliability: Reliability = ba("Reliability", "Run key, retries, replay")
    observability: Observability = ba("Observability", "Alerts and SLAs")
    workload: Workload = ba("Workload", "Expected sizes, for engine choice and cost")
    cost: Cost = ba("Cost", "Budgets")
    artifacts: dict[Identifier, ArtifactOverride] = ba(
        "Artifact overrides", "Raise durability of named artifacts", default_factory=dict
    )
    open_questions: list[str] = ba("Open questions", "Must be empty before generation", default_factory=list)
