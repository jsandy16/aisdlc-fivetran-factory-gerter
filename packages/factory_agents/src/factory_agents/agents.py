"""The four LLM agents: their typed answers and session limits (ADR-0019). Everything else is deterministic code."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .runner import AgentSpec


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PatchProposal(_M):
    file: str = Field(description="Spec file, e.g. pipeline.yaml or rules.yaml")
    op: Literal["set", "add", "remove"]
    path: str = Field(description="Dotted path, list items by id: rules[r_check].plain_english")
    value_json: str = Field(description="The value as JSON (null for remove)")
    evidence: str = Field(description="Short verbatim quote from the requirement supporting the change")


class Question(_M):
    topic: str
    question: str = Field(description="One plain-English question for the business owner")


class IntakeAnswer(_M):
    patches: list[PatchProposal]
    open_questions: list[Question]


class RuleImplAnswer(_M):
    builtin_op: str | None = Field(description="A catalog op that fits the rule, or null for custom code")
    params_json: str = Field(description="The built-in op's params as a JSON object; '{}' for custom code")
    code: str = Field(description="rules/<rule_id>/impl.py; empty when a built-in op is used")
    tests: str = Field(description="rules/<rule_id>/test_impl.py (pytest); empty when a built-in op is used")
    notes: str


class Finding(_M):
    severity: Literal["blocker", "major", "minor"]
    category: Literal["intent", "correctness", "security", "idempotency", "determinism", "cost"]
    message: str


class ReviewAnswer(_M):
    verdict: Literal["approve", "reject"]
    findings: list[Finding]
    summary: str


class EdgeCase(_M):
    name: str
    given_json: str = Field(description="Inputs as a JSON object keyed by input artifact name")
    expect_json: str = Field(description='Expected outcome as JSON: {"value": ...} or {"result": "fail"}')
    why: str


class EdgeCasesAnswer(_M):
    cases: list[EdgeCase]
    questions: list[str]


INTAKE = AgentSpec("intake", "spec-authoring", IntakeAnswer, max_turns=8, budget_usd=0.5)
IMPLEMENT = AgentSpec("rule_implementation", "rule-implementation", RuleImplAnswer, max_turns=10, budget_usd=1.0)
REVIEW = AgentSpec("rule_review", "rule-review", ReviewAnswer, max_turns=6, budget_usd=0.5)
TESTS = AgentSpec("test_generation", "test-generation", EdgeCasesAnswer, max_turns=6, budget_usd=0.3)
