"""rules.yaml: every integration and custom step is a Rule (plain English + a structured implementation).

The category is a discriminator; each category allows a fixed set of built-in ops (from the op catalog), `expression`
(validate and naming only) and `custom` code. Planned ops are accepted by the schema and rejected by the validator with
`op_not_available`, so the schema already shows what is coming.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import Field, JsonValue, create_model, field_validator

from ..catalog import ARTIFACT_KINDS, CATEGORIES, ops_for_category
from ..common import StrictModel, ba
from ..fieldtypes import CelExpr, Identifier, RequirementId, Sha256Hex
from ..ops import OP_PARAMS

ArtifactKind = Literal[ARTIFACT_KINDS]  # type: ignore[valid-type]
EXPRESSION_CATEGORIES = frozenset({"validate", "naming"})


def _no_floats(v: Any, loc: str = "") -> None:
    if isinstance(v, float):
        raise ValueError(f"use whole numbers or text in test cases ({loc or 'value'} is a decimal number)")
    if isinstance(v, dict):
        for k, x in v.items():
            _no_floats(x, f"{loc}.{k}" if loc else k)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _no_floats(x, f"{loc}[{i}]")


class TestCase(StrictModel):
    """An example the owner agreed on: given these inputs, expect this outcome."""

    name: Identifier = ba("Test name", "Short name of the example")
    given: dict[str, JsonValue] = ba("Given", "Inputs of the example", default_factory=dict)
    expect: dict[str, JsonValue] = ba("Expect", "Expected outcome", default_factory=dict)

    @field_validator("given", "expect")
    @classmethod
    def _plain_values(cls, v: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _no_floats(v)
        return v


class OnFailure(StrictModel):
    action: Literal["fail", "quarantine", "warn"] = ba(
        "When it fails", "Stop the run, quarantine, or only warn", default="fail"
    )
    error_class: Literal["data", "config", "security"] = ba(
        "Kind of failure", "Routes the alert: data owner, platform/engineer, or security", default="data"
    )


class ExpressionImpl(StrictModel):
    """A CEL expression: a true/false check (validate) or a computed value (naming)."""

    kind: Literal["expression"]
    cel: CelExpr = ba("Expression", "CEL expression over artifacts, vars and run")


class CustomImpl(StrictModel):
    """Custom code under rules/<rule_id>/ (agent-authored, reviewed, accepted, committed)."""

    kind: Literal["custom"]
    code_sha256: Sha256Hex = ba("Code hash", "sha256 of rules/<rule_id>/impl.py")
    output_kinds: dict[Identifier, ArtifactKind] = ba(
        "Output kinds", "Kind of each artifact the code produces", default_factory=dict
    )


def _builtin_model(op: str) -> Any:
    params_model = OP_PARAMS[op]
    try:
        params_model()
        params_field: Any = (params_model, Field(default_factory=params_model))
    except Exception:  # the op has required params
        params_field = (params_model, ...)
    fields: dict[str, Any] = {"kind": (Literal["builtin"], ...), "op": (Literal[op], ...), "params": params_field}
    return create_model("Builtin_" + op.replace(".", "_"), __base__=StrictModel, __doc__=f"Built-in op {op}.", **fields)


def _planned_model(category: str, ops: list[str]) -> Any:
    fields: dict[str, Any] = {
        "kind": (Literal["builtin"], ...),
        "op": (Literal[tuple(ops)], ...),
        "params": (dict[str, JsonValue], Field(default_factory=dict)),
    }
    return create_model(
        f"Planned_{category}",
        __base__=StrictModel,
        __doc__="A planned op: accepted by the schema, rejected by the validator until it is available.",
        **fields,
    )


_BUILTIN_MODELS: dict[str, Any] = {op: _builtin_model(op) for op in sorted(OP_PARAMS)}


def _implementation_union(category: str) -> Any:
    available = [o.op for o in ops_for_category(category, status="available")]
    planned = [o.op for o in ops_for_category(category, status="planned")]
    builtins: list[Any] = [_BUILTIN_MODELS[op] for op in available]
    if planned:
        builtins.append(_planned_model(category, planned))
    members: list[Any] = []
    if len(builtins) == 1:
        members.append(builtins[0])
    elif builtins:
        members.append(Annotated[Union[tuple(builtins)], Field(discriminator="op")])  # noqa: UP007
    if category in EXPRESSION_CATEGORIES:
        members.append(ExpressionImpl)
    members.append(CustomImpl)
    return Annotated[Union[tuple(members)], Field(discriminator="kind")]  # noqa: UP007


class RuleBase(StrictModel):
    rule_id: Identifier = ba("Rule id", "Stable id, e.g. r_zip")
    plain_english: str = ba(
        "What the rule does (plain English)",
        "The business wording, kept for traceability",
        min_length=10,
        max_length=2000,
    )
    inputs: list[Identifier] = ba("Inputs", "Artifacts this rule reads", default_factory=list)
    outputs: list[Identifier] = ba("Outputs", "Artifacts this rule produces", default_factory=list)
    preconditions: list[CelExpr] = ba(
        "Preconditions", "CEL checks that must hold before the rule runs", default_factory=list
    )
    on_failure: OnFailure = ba("On failure", "What happens when the rule fails", default_factory=OnFailure)
    test_cases: list[TestCase] = ba("Examples", "Owner-approved examples (the test oracle)", default_factory=list)
    requirements: list[RequirementId] = ba("Requirements", "Requirement ids this rule implements", default_factory=list)


def _rule_model(category: str) -> Any:
    fields: dict[str, Any] = {
        "category": (Literal[category], ...),
        "implementation": (_implementation_union(category), ...),
    }
    return create_model(
        f"{category.capitalize()}Rule", __base__=RuleBase, __doc__=f"A rule of category {category}.", **fields
    )


RULE_MODELS: dict[str, Any] = {c: _rule_model(c) for c in CATEGORIES}
Rule: Any = Annotated[Union[tuple(RULE_MODELS[c] for c in CATEGORIES)], Field(discriminator="category")]  # noqa: UP007


class RulesFile(StrictModel):
    """rules.yaml: owned by the business owner / BA."""

    rules: list[Rule] = ba("Rules", "Every step of the pipeline, in plain English and structured form", min_length=1)
