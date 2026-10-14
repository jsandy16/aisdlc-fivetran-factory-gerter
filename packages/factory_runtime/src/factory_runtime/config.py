"""Runtime configuration: where this task runs and which stores it uses. Never secrets.

Production reads it from FACTORY_* environment variables set by the generated pod spec or Lambda configuration
(Phase 5). Tests build it directly. Secrets are never configuration: they are resolved through the SecretProvider.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory_core.errors import ConfigError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

_ID = r"^[a-z][a-z0-9_]*$"
_BUCKET = r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    env: str = Field(pattern=r"^[a-z][a-z0-9]{1,9}$")
    team: str = Field(pattern=_ID)
    pipeline_id: str = Field(pattern=_ID)
    region: str = Field(pattern=r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
    staging_bucket: str = Field(pattern=_BUCKET)
    run_state_table: str = Field(pattern=r"^[A-Za-z0-9_.-]{3,255}$")
    bundle_store: str = Field(pattern=_BUCKET)
    audit_store: str = Field(pattern=_BUCKET)
    staging_kms_key: str | None = None
    audit_kms_key: str | None = None
    image_digest: str = "unknown"
    placement_accounts: dict[str, str] = Field(default_factory=dict)  # placement alias -> 12-digit account id
    resources: dict[str, str] = Field(default_factory=dict)  # declared resource id -> ARN (environments.yaml)
    vault_address: str | None = Field(default=None, pattern=r"^https://[a-z0-9.-]+(:\d{1,5})?$")
    vault_auth_mount: str = "aws"
    vault_role: str | None = None
    vault_iam_server_id: str | None = None
    fivetran_api_secret: str | None = None  # Secrets Manager id of the platform's Fivetran API key (fivetran.sync)
    poll_budget_s: int | None = Field(default=None, ge=30, le=840)  # waits per invocation (Lambda behind a sensor)
    # the dispatcher Lambda only: the deployed bundle it validates messages against, and where it starts runs
    bundle_uri: str | None = None
    bundle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    dag_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,250}$")
    mwaa_environment: str | None = Field(default=None, pattern=r"^[A-Za-z][0-9A-Za-z_-]{0,79}$")
    trigger_role_arn: str | None = Field(
        default=None, pattern=r"^arn:aws[a-z-]*:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$"
    )
    workdir: Path = Path("/tmp/factory")  # noqa: S108 - Lambda/pod ephemeral storage; private per invocation
    # local runs only (`factory run`): every secret comes from the developer's file; refused in ppt and prod
    local_secrets_file: Path | None = None

    @property
    def bundle_prefix(self) -> str:
        return f"{self.team}/{self.pipeline_id}/"


_ENV_MAP = {
    "env": "FACTORY_ENV",
    "team": "FACTORY_TEAM",
    "pipeline_id": "FACTORY_PIPELINE_ID",
    "region": "FACTORY_REGION",
    "staging_bucket": "FACTORY_STAGING_BUCKET",
    "run_state_table": "FACTORY_RUN_STATE_TABLE",
    "bundle_store": "FACTORY_BUNDLE_STORE",
    "audit_store": "FACTORY_AUDIT_STORE",
    "staging_kms_key": "FACTORY_STAGING_KMS_KEY",
    "audit_kms_key": "FACTORY_AUDIT_KMS_KEY",
    "image_digest": "FACTORY_IMAGE_DIGEST",
    "placement_accounts": "FACTORY_PLACEMENT_ACCOUNTS",
    "resources": "FACTORY_RESOURCES",
    "vault_address": "FACTORY_VAULT_ADDRESS",
    "vault_auth_mount": "FACTORY_VAULT_AUTH_MOUNT",
    "vault_role": "FACTORY_VAULT_ROLE",
    "vault_iam_server_id": "FACTORY_VAULT_IAM_SERVER_ID",
    "fivetran_api_secret": "FACTORY_FIVETRAN_API_SECRET",
    "poll_budget_s": "FACTORY_POLL_BUDGET_S",
    "bundle_uri": "FACTORY_BUNDLE_URI",
    "bundle_sha256": "FACTORY_BUNDLE_SHA256",
    "dag_id": "FACTORY_DAG_ID",
    "mwaa_environment": "FACTORY_MWAA_ENV",
    "trigger_role_arn": "FACTORY_TRIGGER_ROLE_ARN",
    "workdir": "FACTORY_WORKDIR",
    "local_secrets_file": "FACTORY_LOCAL_SECRETS_FILE",
}
ENV_VARS = frozenset(_ENV_MAP.values())


def from_environ(environ: Mapping[str, str]) -> RuntimeConfig:
    raw: dict[str, Any] = {}
    for fname, var in _ENV_MAP.items():
        if var in environ and environ[var] != "":
            raw[fname] = environ[var]
    if "resources" in raw:
        try:
            raw["resources"] = json.loads(raw["resources"])
        except json.JSONDecodeError:
            raise ConfigError("runtime_config_invalid", "FACTORY_RESOURCES is not JSON") from None
    if "placement_accounts" in raw:
        try:
            raw["placement_accounts"] = json.loads(raw["placement_accounts"])
        except json.JSONDecodeError:
            raise ConfigError("runtime_config_invalid", "FACTORY_PLACEMENT_ACCOUNTS is not JSON") from None
    try:
        return RuntimeConfig.model_validate(raw)
    except ValidationError as exc:
        fields = sorted({_ENV_MAP.get(str(e["loc"][0]), str(e["loc"][0])) for e in exc.errors(include_input=False)})
        raise ConfigError("runtime_config_invalid", f"missing or invalid settings: {', '.join(fields)}") from None


@dataclass(frozen=True)
class Overrides:
    """Test-only hooks, accepted only through the in-process API (run.main(..., overrides=...)).

    The production entry points (`python -m factory_runtime.run`, the Lambda handlers) always pass None. Hostnames
    are never overridden here: tests map them in the resolver (factory_testing.netmap), so TLS still checks the real
    name.
    """

    aws_endpoint_url: str | None = None
    extra_ca_pem: bytes | None = None
    clock: Callable[[], float] | None = None
    sleep: Callable[[float], Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # local runs (`factory run`): build the AWS client factory (framework stores local, data in the dev account)
    aws_clients: Callable[..., Any] | None = None
