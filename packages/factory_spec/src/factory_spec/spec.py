"""PipelineSpec: the six validated files of one pipeline, with id indexes for cross-file checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sections.destinations import DestinationsFile
from .sections.environments import EnvironmentsFile
from .sections.integrations import IntegrationsFile
from .sections.pipeline import PipelineFile, Task
from .sections.rules import RulesFile
from .sections.security import SecurityFile


@dataclass(frozen=True)
class PipelineSpec:
    path: Path
    pipeline: PipelineFile
    rules: RulesFile
    integrations: IntegrationsFile
    security: SecurityFile
    environments: EnvironmentsFile
    destinations: DestinationsFile
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def pipeline_id(self) -> str:
        return self.pipeline.metadata.pipeline_id

    def rule(self, rule_id: str) -> Any | None:
        return next((r for r in self.rules.rules if r.rule_id == rule_id), None)

    def task_of_rule(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for t in self.pipeline.orchestration.tasks:
            for r in t.rules:
                out.setdefault(r, t.task_id)
        return out

    def tasks(self) -> list[Task]:
        return list(self.pipeline.orchestration.tasks)

    def ordered_rules(self) -> list[tuple[Task, Any]]:
        """(task, rule) pairs in execution order: tasks topologically (ties by task_id), rules in task order."""
        tasks = {t.task_id: t for t in self.pipeline.orchestration.tasks}
        done: list[str] = []
        pending = sorted(tasks)
        while pending:
            ready = [t for t in pending if all(d in done or d not in tasks for d in tasks[t].depends_on)]
            if not ready:  # cycle: validators report it; fall back to id order
                ready = pending[:1]
            for t in ready:
                done.append(t)
                pending.remove(t)
        out: list[tuple[Task, Any]] = []
        for tid in done:
            for rid in tasks[tid].rules:
                r = self.rule(rid)
                if r is not None:
                    out.append((tasks[tid], r))
        return out

    def upstream(self, task_id: str) -> set[str]:
        tasks = {t.task_id: t for t in self.pipeline.orchestration.tasks}
        seen: set[str] = set()
        stack = list(tasks[task_id].depends_on) if task_id in tasks else []
        while stack:
            t = stack.pop()
            if t in seen or t not in tasks:
                continue
            seen.add(t)
            stack.extend(tasks[t].depends_on)
        return seen
