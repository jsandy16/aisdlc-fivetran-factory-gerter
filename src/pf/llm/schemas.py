"""Output contracts for every LLM step. The agent's answer must validate against these, or the step fails."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- English rule -> CEL
class CaseValue(_M):
    name: str = Field(description="Column (row/attribute rules) or package fact name")
    value_json: str = Field(description="The value as JSON, e.g. 42, \"2024-01-31\", null")


class GeneratedCase(_M):
    values: list[CaseValue]
    expected: Literal["pass", "fail"]
    why: str = Field(description="Which clause or boundary this case tests")


class CompiledRule(_M):
    expression: str = Field(description="CEL expression; true when the data passes")
    referenced: list[str] = Field(description="Every row.<column>, `value` or package.<fact> the expression uses")
    ambiguities: list[str] = Field(description="Questions the owner must answer first; empty if the rule is unambiguous")
    cases: list[GeneratedCase] = Field(description="Boundary and edge cases: nulls, equality, units, empty strings")


class Explanation(_M):
    explanation: str = Field(description="What the expression checks, in plain English, for a business reader")
    passes_when: list[str]
    fails_when: list[str]


# --------------------------------------------------------------------------- requirement document -> spec proposals
SpecFileName = Literal["pipeline.yaml", "source.yaml", "destination.yaml", "sttm.yaml", "quality.yaml", "policy.yaml"]


class FieldProposal(_M):
    file: SpecFileName
    path: str = Field(description="Dotted path inside the file, e.g. source.host or requirement.purpose")
    value_json: str = Field(description="Proposed value as JSON")
    evidence: str = Field(description="Short verbatim quote from the document that supports the value")
    confidence: Literal["high", "medium", "low"]


class CheckProposal(_M):
    id: str = Field(description="snake_case check id")
    kind: Literal["regex", "allowed_values", "range", "length"]
    pattern: str | None = Field(description="regex kind: full-match Python regex")
    values: list[str] = Field(description="allowed_values kind: the allowed values; empty otherwise")
    min: float | None = Field(description="range: smallest allowed value; length: shortest length")
    max: float | None = Field(description="range: largest allowed value; length: longest length")
    description: str
    evidence: str


class AttributeProposal(_M):
    name: str = Field(description="snake_case target column")
    source_column: str
    type: Literal["STRING", "INT", "LONG", "DOUBLE", "DECIMAL", "BOOLEAN", "NAIVE_DATE", "NAIVE_DATETIME", "UTC_DATETIME", "JSON"]
    precision: int | None
    scale: int | None
    input_format: str | None = Field(description="strptime format if the document states one")
    nullable: bool
    description: str
    business_rule: str
    checks: list[CheckProposal] = Field(description="Every constraint on this attribute's own value: formats/digits (regex), "
                                                    "closed value lists (allowed_values), numeric bounds incl. 'never negative' (range), lengths")
    evidence: str


class EntityProposal(_M):
    name: str = Field(description="snake_case target table")
    description: str
    member_pattern: str | None = Field(description="Regex for the file inside a delivery (file sources)")
    table: str | None = Field(description="schema.table (database sources)")
    cursor_column: str | None
    primary_key: list[str]
    attributes: list[AttributeProposal]
    evidence: str


class RuleProposal(_M):
    entity: str
    attribute: str | None = Field(description="null for a cross-attribute (row) rule")
    id: str = Field(description="snake_case rule id")
    text: str = Field(description="The rule in the document's words")
    evidence: str


class RequirementProposal(_M):
    fields: list[FieldProposal]
    entities: list[EntityProposal]
    english_rules: list[RuleProposal] = Field(description="ONLY rules no attribute check kind can express: across columns, "
                                                          "conditional, or date arithmetic. Never formats, value lists, bounds, "
                                                          "mandatory or unique")
    open_questions: list[str] = Field(description="What the document leaves unclear or contradicts")
