"""The six spec files, their models, owners and whether they are logic or environment bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .common import StrictModel
from .sections.destinations import DestinationsFile
from .sections.environments import EnvironmentsFile
from .sections.integrations import IntegrationsFile
from .sections.pipeline import PipelineFile
from .sections.rules import RulesFile
from .sections.security import SecurityFile


@dataclass(frozen=True)
class SpecFile:
    name: str
    model: type[StrictModel]
    owner_role: str
    hash_kind: Literal["logic", "binding"]
    ba_label: str


SPEC_FILES: dict[str, SpecFile] = {
    f.name: f
    for f in (
        SpecFile(
            "pipeline.yaml", PipelineFile, "data_engineer", "logic", "Pipeline: identity, trigger, tasks, reliability"
        ),
        SpecFile("rules.yaml", RulesFile, "business_owner", "logic", "Rules: every step in plain English"),
        SpecFile(
            "integrations.yaml",
            IntegrationsFile,
            "data_engineer",
            "logic",
            "Integrations: sources, portal, destinations, secrets",
        ),
        SpecFile(
            "security.yaml", SecurityFile, "governance", "logic", "Security: classification, GxP, audit, retention"
        ),
        SpecFile(
            "environments.yaml", EnvironmentsFile, "platform", "binding", "Environments: per-environment bindings"
        ),
        SpecFile(
            "destinations.yaml", DestinationsFile, "governance", "binding", "Destinations: approved delivery targets"
        ),
    )
}
SPEC_DIR = "spec"
