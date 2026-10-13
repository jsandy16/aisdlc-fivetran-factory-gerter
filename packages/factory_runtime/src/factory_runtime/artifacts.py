"""Artifacts of a running task, run variables and run facts.

In-task artifacts live in memory (streams and local values); staged artifacts are read lazily from staging, so a task
only loads the upstream artifacts its rules actually use. Staged documents are named `<artifact>.json`; a staged
package is `<artifact>.zip` with its hash record `<artifact>.meta.json` (written first).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO, Any

from factory_core.errors import ConfigError
from factory_spec.artifact_models import ARTIFACT_MODELS, PortalSnapshot, TriggerSnapshot, facet
from factory_spec.derive import ArtifactGraph
from factory_spec.spec import PipelineSpec

from .staging import Staging

TRIGGER_SNAPSHOT = "trigger.json"
ADOPT_KINDS = frozenset({"message", "portal_snapshot", "object_listing"})  # custody snapshots: first answer wins


def doc_name(artifact: str) -> str:
    return f"{artifact}.json"


def package_name(artifact: str) -> str:
    return f"{artifact}.zip"


def package_meta_name(artifact: str) -> str:
    return f"{artifact}.meta.json"


@dataclass(frozen=True)
class FileStream:
    """One file of a file_set: a path relative to the package root, its size, and a readable stream."""

    rel_path: str
    size: int
    open: Callable[[], IO[bytes]]
    source_key: str = ""


FileSet = Iterator[FileStream]


@dataclass(frozen=True)
class RunInfo:
    run_key: str
    run_dir: str
    run_key_hash: str
    env: str
    pipeline_id: str
    team: str
    attempt: int
    run_id: str
    pinned_at: int
    run_date: str

    def cel(self) -> dict[str, Any]:
        return {
            "run_key": self.run_key,
            "run_date": self.run_date,
            "env": self.env,
            "pipeline_id": self.pipeline_id,
            "team": self.team,
            "attempt": self.attempt,
        }

    def naming_builtins(self) -> dict[str, str | int]:
        return {"pipeline_id": self.pipeline_id, "team": self.team, "env": self.env, "run_date": self.run_date}


def run_date_of(pinned_at: int, trigger: TriggerSnapshot | None) -> str:
    if trigger is not None and trigger.source == "schedule":
        start = trigger.body.get("data_interval_start")
        if isinstance(start, str) and len(start) >= 10:
            return start[:10]
    return datetime.fromtimestamp(pinned_at, UTC).strftime("%Y-%m-%d")


@dataclass
class Artifact:
    name: str
    kind: str
    durability: str
    value: Any


class ArtifactStore:
    def __init__(self, graph: ArtifactGraph, staging: Staging) -> None:
        self.graph = graph
        self.staging = staging
        self._mem: dict[str, Artifact] = {}

    def kind(self, name: str) -> str:
        node = self.graph.artifacts.get(name)
        if node is None:
            raise ConfigError("artifact_unknown", f"artifact '{name}' is not produced by any rule")
        return node.kind

    def put(self, name: str, value: Any) -> Artifact:
        node = self.graph.artifacts[name]
        art = Artifact(name, node.kind, node.durability, value)
        self._mem[name] = art
        return art

    def has(self, name: str) -> bool:
        return name in self._mem

    def get(self, name: str) -> Artifact:
        art = self._mem.get(name)
        if art is not None:
            return art
        node = self.graph.artifacts.get(name)
        if node is None:
            raise ConfigError("artifact_unknown", f"artifact '{name}' is not produced by any rule")
        if node.durability not in ("staged", "final"):
            raise ConfigError("artifact_unavailable", f"artifact '{name}' is not staged and was not produced yet")
        model = ARTIFACT_MODELS.get(node.kind)
        if node.kind == "package":
            value: Any = self.staging.get_doc(package_meta_name(name), ARTIFACT_MODELS["package"])
        elif model is not None:
            value = self.staging.get_doc(doc_name(name), model)
        else:
            import json

            value = json.loads(self.staging.get_bytes(doc_name(name)))
        return self.put(name, value)

    def facet(self, name: str) -> Any:
        art = self.get(name)
        return facet(art.kind, art.value)


@dataclass
class Vars:
    """Run variables (pipeline.yaml `variables`): from the trigger snapshot, or from the portal answer once known."""

    spec: PipelineSpec
    trigger: TriggerSnapshot | None
    portal: Callable[[str], PortalSnapshot | None]
    _cache: dict[str, Any] = field(default_factory=dict)

    def _source(self, name: str) -> str:
        src = self.spec.pipeline.variables.get(name)
        if src is None:
            raise ConfigError("variable_unknown", f"variable '{name}' is not declared")
        return src

    def get(self, name: str) -> Any:
        if name in self._cache:
            return self._cache[name]
        parts = self._source(name).split(".")
        value: Any
        if parts[0] == "trigger":
            if self.trigger is None:
                raise ConfigError("trigger_snapshot_missing", "the run has no trigger snapshot")
            value = self.trigger.body.get(parts[2])
        else:
            snap = self.portal(parts[1])
            if snap is None:
                raise ConfigError("variable_unavailable", f"variable '{name}' needs the portal answer first")
            value = snap.values.get(parts[2])
        if value is None:
            raise ConfigError("variable_unavailable", f"variable '{name}' has no value in this run")
        self._cache[name] = value
        return value

    def available(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in self.spec.pipeline.variables:
            try:
                out[name] = self.get(name)
            except ConfigError:
                continue
        return out

    def strings(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.available().items()}
