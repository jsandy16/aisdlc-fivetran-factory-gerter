"""pipeline.yaml — the manifest: identity, version, owners, requirement summary, schedule, runtime."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from .common import Environment, Identifier, SemVer, StrictModel

SYNC_FREQUENCIES = (1, 5, 15, 30, 60, 120, 180, 360, 480, 720, 1440)


class Owners(StrictModel):
    """GitHub handles (or team slugs) — used for CODEOWNERS, so each owner approves their own file."""

    product_owner: str = Field(pattern=r"^@[\w.-]+(/[\w.-]+)?$")
    data_engineer: str = Field(pattern=r"^@[\w.-]+(/[\w.-]+)?$")
    governance: str = Field(pattern=r"^@[\w.-]+(/[\w.-]+)?$")
    quality: str = Field(pattern=r"^@[\w.-]+(/[\w.-]+)?$")


class Requirement(StrictModel):
    purpose: str = Field(min_length=10)
    consumers: list[str] = Field(min_length=1)
    freshness_sla_hours: int = Field(ge=1)
    late_data_policy: Literal["wait", "load_late", "reject_late"] = "load_late"
    source_document: str | None = Field(default=None, description="Requirement document this spec was drafted from")


class Schedule(StrictModel):
    sync_frequency: int = Field(default=360, description="Minutes: 1,5,15,30,60,120,180,360,480,720,1440")
    daily_sync_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="UTC HH:MM, only with 1440")
    paused: bool = False

    @field_validator("sync_frequency")
    @classmethod
    def _allowed(cls, v: int) -> int:
        if v not in SYNC_FREQUENCIES:
            raise ValueError(f"sync_frequency must be one of {SYNC_FREQUENCIES}")
        return v

    @model_validator(mode="after")
    def _daily(self) -> "Schedule":
        if self.daily_sync_time and self.sync_frequency != 1440:
            raise ValueError("daily_sync_time applies only when sync_frequency is 1440")
        return self


class Retry(StrictModel):
    attempts: int = Field(default=5, ge=1, le=10)
    base_seconds: float = Field(default=2.0, ge=0.1, le=60)
    max_seconds: float = Field(default=60.0, ge=1, le=900)


class Runtime(StrictModel):
    parallelism: int = Field(default=2, ge=1, le=8, description="Packages prepared in parallel; emitted in order")
    retry: Retry = Field(default_factory=Retry)
    checkpoint_every_rows: int = Field(default=5000, ge=100, le=1_000_000)
    python_version: Literal["3.10", "3.11", "3.12", "3.13", "3.14"] = "3.12"


class PipelineFile(StrictModel):
    """pipeline.yaml"""

    api_version: Literal["pf/v1"] = "pf/v1"
    name: Identifier = Field(description="Fivetran connection name = destination schema (^[_a-z][_a-z0-9]*$)")
    version: SemVer = "0.1.0"
    title: str = Field(min_length=3)
    gxp_tier: Literal["non_gxp", "gxp_indirect", "gxp_direct"]
    owners: Owners
    requirement: Requirement
    environments: list[Environment] = Field(min_length=1)
    schedule: dict[Environment, Schedule] = Field(default_factory=dict)
    runtime: Runtime = Field(default_factory=Runtime)

    @model_validator(mode="after")
    def _schedule_envs(self) -> "PipelineFile":
        unknown = set(self.schedule) - set(self.environments)
        if unknown:
            raise ValueError(f"schedule has environments not listed in environments: {sorted(unknown)}")
        return self
