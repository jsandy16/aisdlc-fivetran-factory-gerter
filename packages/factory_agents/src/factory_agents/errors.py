"""Errors of the agent layer: a failed call, and the typed escalation handed to a person."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class AgentError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Escalation(BaseModel):
    """What a person sees when an agent could not produce an acceptable result within its attempts or budget."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: str
    attempts: int
    reason: str
    last_issues: list[str]
    budget_used_usd: float
    artefacts: dict[str, Any] = {}
    recommended_human_action: str
