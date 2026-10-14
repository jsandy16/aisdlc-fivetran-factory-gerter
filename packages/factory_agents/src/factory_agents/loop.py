"""Bounded self-correction (agents-and-gates.md §3): at most 3 attempts, early stop on a repeated issue signature or
an exhausted budget, then a typed Escalation. Feedback to the agent is structured issues only, never free text from
another agent."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from .errors import Escalation

R = TypeVar("R")
MAX_ATTEMPTS = 3


@dataclass
class Outcome(Generic[R]):
    result: R | None
    escalation: Escalation | None
    attempts: int


def signature(issues: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(issues)).encode()).hexdigest()


def correct(
    agent: str,
    attempt_fn: Callable[[list[str], int], tuple[R, list[str], float]],
    *,
    budget_usd: float,
    action: str,
    artefacts: Callable[[], dict[str, Any]] | None = None,
) -> Outcome[R]:
    """attempt_fn(feedback, attempt) -> (result, issues, cost_usd). Accept the first result without issues."""
    feedback: list[str] = []
    previous = ""
    spent = 0.0
    issues: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result, issues, cost = attempt_fn(feedback, attempt)
        spent += cost
        if not issues:
            return Outcome(result, None, attempt)
        sig = signature(issues)
        reason = ""
        if attempt == MAX_ATTEMPTS:
            reason = "attempts exhausted"
        elif sig == previous:
            reason = "the same issues came back"
        elif spent >= budget_usd:
            reason = "budget exhausted"
        if reason:
            esc = Escalation(agent=agent, attempts=attempt, reason=reason, last_issues=issues, budget_used_usd=spent,
                             artefacts=artefacts() if artefacts else {}, recommended_human_action=action)  # fmt: skip
            return Outcome(None, esc, attempt)
        previous, feedback = sig, issues
    raise AssertionError("unreachable")  # pragma: no cover
