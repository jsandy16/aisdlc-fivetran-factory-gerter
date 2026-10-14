"""Names shared by the DAG and Terraform generators, so both sides always refer to the same resources."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


def _clip(name: str, limit: int) -> str:
    if len(name) <= limit:
        return name
    return name[: limit - 9] + "-" + hashlib.sha256(name.encode()).hexdigest()[:8]


def _kebab(s: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", s.lower())


def dag_id(pipeline_id: str, spec_version: str) -> str:
    """ADR-0018: a breaking change bumps the major version and gets a new DAG; majors 0 and 1 keep the plain id."""
    major = int(spec_version.split(".", 1)[0])
    return pipeline_id if major <= 1 else f"{pipeline_id}__v{major}"


def lambda_function(team: str, pipeline_id: str, task_id: str) -> str:
    return _clip(_kebab(f"factory-{team}-{pipeline_id}-{task_id}"), 64)


def dispatcher_function(team: str, pipeline_id: str) -> str:
    return _clip(_kebab(f"factory-{team}-{pipeline_id}-dispatch"), 64)


def role(team: str, pipeline_id: str, task_id: str) -> str:
    return _clip(_kebab(f"factory-{team}-{pipeline_id}-{task_id}"), 64)


def service_account(pipeline_id: str, task_id: str) -> str:
    return _clip(_kebab(f"{pipeline_id}-{task_id}"), 63)


def namespace(team: str) -> str:
    return _clip(_kebab(f"factory-{team}"), 63)


@dataclass(frozen=True)
class Deploy:
    """Per-environment deploy facts the generators need; real values arrive with the deploy (Phase 8)."""

    env: str
    team: str
    region: str
    bundle_uri: str
    bundle_sha256: str
    image: str
    staging_bucket: str
    run_state_table: str
    bundle_store: str
    audit_store: str

    @classmethod
    def placeholder(cls, env: str, team: str, region: str, pipeline_id: str, bundle_sha256: str) -> Deploy:
        t = _kebab(team)
        return cls(
            env=env, team=team, region=region,
            bundle_uri=f"s3://factory-{t}-{env}-bundles/{team}/{pipeline_id}/{bundle_sha256}.zip",
            bundle_sha256=bundle_sha256,
            image="factory-runtime@sha256:" + "0" * 64,
            staging_bucket=f"factory-{t}-{env}-staging", run_state_table=f"factory-{t}-{env}-run-state",
            bundle_store=f"factory-{t}-{env}-bundles", audit_store=f"factory-{t}-{env}-audit",
        )  # fmt: skip


def deploy_for(platform: Any, team: str, pipeline_id: str, bundle_sha256: str, image: str | None = None) -> Deploy:
    """Deploy facts from the platform settings (stores, region); the image digest arrives with the release."""
    t = platform.team_stores(team)
    return Deploy(
        env=platform.environment,
        team=team,
        region=platform.region,
        bundle_uri=f"s3://{t.bundle_store}/{team}/{pipeline_id}/{bundle_sha256}.zip",
        bundle_sha256=bundle_sha256,
        image=image or "factory-runtime@sha256:" + "0" * 64,
        staging_bucket=t.staging_bucket,
        run_state_table=t.run_state_table,
        bundle_store=t.bundle_store,
        audit_store=t.audit_store,
    )
