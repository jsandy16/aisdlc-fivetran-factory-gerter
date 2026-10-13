"""RuleContext: what an op (built-in or custom) sees while it runs one rule."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory_core.errors import ConfigError
from factory_spec.spec import PipelineSpec

from .artifacts import Artifact, ArtifactStore, RunInfo, Vars
from .audit import AuditLog
from .services import Services
from .staging import Staging


@dataclass
class RuleContext:
    rule: Any
    task_id: str
    spec: PipelineSpec
    services: Services
    run: RunInfo
    vars: Vars
    artifacts: ArtifactStore
    staging: Staging
    audit: AuditLog
    workdir: Path
    secrets: Any = None  # secrets.router.Secrets; never handed to custom rules
    staged: set[str] = field(default_factory=set)  # outputs the op already persisted itself

    @property
    def params(self) -> Any:
        return self.rule.implementation.params

    @property
    def env(self) -> str:
        return self.run.env

    @property
    def binding(self) -> Any:
        return self.spec.environments.environments[self.run.env]

    def inputs(self) -> list[Artifact]:
        return [self.artifacts.get(n) for n in self.rule.inputs]

    def input_of(self, kind: str, *, required: bool = True) -> Any:
        """The value of the (first) input of this kind, as bound by the op contract."""
        for name in self.rule.inputs:
            if self.artifacts.kind(name) == kind:
                return self.artifacts.get(name).value
        if required:
            raise ConfigError("input_missing", f"rule {self.rule.rule_id} has no input of kind {kind}")
        return None

    def input_name(self, kind: str) -> str | None:
        return next((n for n in self.rule.inputs if self.artifacts.kind(n) == kind), None)

    def output_name(self, index: int = 0) -> str:
        return str(self.rule.outputs[index])

    def cel_activation(self) -> dict[str, Any]:
        return {
            "artifacts": {n: self.artifacts.facet(n) for n in self.rule.inputs},
            "vars": self.vars.available(),
            "run": self.run.cel(),
        }

    def now(self) -> int:
        """The run's clock: pinned when the run started, identical for every task and retry."""
        return self.run.pinned_at

    def log(self, event: str, **fields: Any) -> None:
        self.services.log.event(event, rule_id=self.rule.rule_id, **fields)
