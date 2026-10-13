"""Op contracts: one declaration per op drives validation, dataflow, engine feasibility, IAM, egress and docs."""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Literal

from factory_core.canonical import doc_sha256
from pydantic import BaseModel, ConfigDict, Field

CATEGORIES = (
    "extract",
    "transform",
    "validate",
    "package",
    "encrypt",
    "checksum",
    "naming",
    "upload",
    "housekeeping",
    "integration",
)
ARTIFACT_KINDS = (
    "message",
    "portal_snapshot",
    "destination_set",
    "object_listing",
    "file_set",
    "package",
    "manifest",
    "checksum",
    "value",
    "report",
    "receipt",
)
ENGINES = ("lambda", "eks_pod", "eks_pod_spot", "mwaa", "fivetran_native", "fivetran_sdk")
Durability = Literal["stream", "local", "staged", "final"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Port(_M):
    port: str
    kind: str
    required: bool = True
    streaming: bool = False
    min_durability: Durability = "local"


class AwsAction(_M):
    actions: list[str]
    resource: str


class Egress(_M):
    host: str
    port: str


class EngineResources(_M):
    bytes_per_s: int | None = None  # throughput of the byte-moving part; None = moves no bulk bytes
    overhead_s: int = 5


class OpContract(_M):
    op: str
    version: str
    status: Literal["available", "planned"]
    categories: list[str]
    summary: str
    inputs: list[Port] = Field(default_factory=list)
    outputs: list[Port] = Field(default_factory=list)
    allowed_engines: list[str] = Field(default_factory=list)
    side_effects: list[str] = Field(default_factory=list)
    aws_actions: list[AwsAction] = Field(default_factory=list)
    egress: list[Egress] = Field(default_factory=list)
    determinism: Literal["pure", "deterministic_effect", "nondeterministic"] = "pure"
    idempotency: Literal["natural", "conditional_write", "dedupe_key"] = "natural"
    resources: dict[str, EngineResources] = Field(default_factory=dict)

    @property
    def contract_sha(self) -> str:
        return doc_sha256("factory.op_contract", self.model_dump(mode="json"))


class LambdaLimits(_M):
    timeout_s: int
    memory_bytes: int
    tmp_bytes: int
    safety_factor: int


class EngineTable(_M):
    version: str
    lambda_: LambdaLimits = Field(alias="lambda")
    max_package_bytes: int
    delivery_bytes_per_s: dict[str, int]

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def _load(name: str) -> object:
    return json.loads(resources.files("factory_spec.catalog").joinpath(name).read_text(encoding="utf-8"))


@cache
def op_catalog() -> dict[str, OpContract]:
    raw = _load("op_catalog.v1.json")
    if not isinstance(raw, dict):
        raise TypeError("op_catalog.v1.json must hold an object")
    ops = [OpContract.model_validate(o) for o in raw["ops"]]
    return {o.op: o for o in sorted(ops, key=lambda o: o.op)}


@cache
def engine_table() -> EngineTable:
    return EngineTable.model_validate(_load("engine_table.v1.json"))


def ops_for_category(category: str, *, status: str | None = None) -> list[OpContract]:
    return [o for o in op_catalog().values() if category in o.categories and (status is None or o.status == status)]
