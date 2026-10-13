"""Models and data: the valid example, catalog consistency, dynamic models, units, YAML round-trip and the BA view."""

from __future__ import annotations

import typing
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from factory_spec.ba_view import render_ba
from factory_spec.catalog import ARTIFACT_KINDS, CATEGORIES, ENGINES, engine_table, op_catalog, ops_for_category
from factory_spec.dynamic import build_model, record_model
from factory_spec.load import load_spec
from factory_spec.ops import OP_PARAMS
from factory_spec.sections.fields import FieldSpec, RecordSpec
from factory_spec.sections.pipeline import Cost, Workload
from factory_spec.sections.rules import RULE_MODELS
from factory_spec.validate import validate_dir
from pydantic import ValidationError
from spec_helpers import EXAMPLE_REPO, PIPELINE, REPO_ROOT

from factory_core import yamlio

STARTERS = REPO_ROOT / "packages" / "factory" / "src" / "factory" / "starters"
EXAMPLE = EXAMPLE_REPO / "pipelines" / PIPELINE

# ---------------------------------------------------------------- the valid example


def test_worked_example_is_valid_without_findings() -> None:
    result = validate_dir(EXAMPLE)
    assert result.ok
    assert [(i.code, i.loc) for i in result.issues] == []


def test_worked_example_shape() -> None:
    spec = load_spec(EXAMPLE).spec
    assert spec is not None
    assert [t.task_id for t in spec.pipeline.orchestration.tasks] == ["t1_collect", "t2_checksum", "t3_upload"]
    assert [r.rule_id for _, r in spec.ordered_rules()][:3] == [
        "r_read_request",
        "r_portal_lookup",
        "r_resolve_destinations",
    ]
    assert spec.upstream("t3_upload") == {"t1_collect", "t2_checksum"}
    assert sorted(spec.destinations.destinations) == ["dev", "ppt", "prod", "test"]


# ---------------------------------------------------------------- catalog


def test_catalog_categories_and_rule_unions_agree() -> None:
    assert len(CATEGORIES) == 10
    assert set(RULE_MODELS) == set(CATEGORIES)
    for o in op_catalog().values():
        assert set(o.categories) <= set(CATEGORIES), o.op
        assert set(o.allowed_engines) <= set(ENGINES), o.op
        assert {p.kind for p in [*o.inputs, *o.outputs]} <= set(ARTIFACT_KINDS), o.op


def test_every_available_op_has_params_and_planned_ops_do_not() -> None:
    available = {o.op for o in op_catalog().values() if o.status == "available"}
    planned = {o.op for o in op_catalog().values() if o.status == "planned"}
    assert set(OP_PARAMS) == available
    assert not planned & set(OP_PARAMS)
    assert {"crypto.encrypt", "crypto.decrypt"} <= planned  # encryption deferred (ADR-0012)
    for o in op_catalog().values():
        if o.status == "available":
            assert o.allowed_engines, o.op
            assert o.outputs or o.op.startswith("validate."), o.op


def _union_ops(model: Any) -> set[str]:
    """All op literals accepted by a category's rule model."""
    impl = model.model_fields["implementation"].annotation
    ops: set[str] = set()

    def walk(t: Any) -> None:
        for a in typing.get_args(t):
            walk(a)
        if isinstance(t, type) and hasattr(t, "model_fields") and "op" in t.model_fields:
            ops.update(typing.get_args(t.model_fields["op"].annotation))

    walk(impl)
    return ops


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_category_union_matches_catalog(category: str) -> None:
    assert _union_ops(RULE_MODELS[category]) == {o.op for o in ops_for_category(category)}


def test_contract_sha_is_stable_and_sensitive() -> None:
    c = op_catalog()["package.zip"]
    assert c.contract_sha == op_catalog()["package.zip"].contract_sha
    changed = c.model_copy(update={"allowed_engines": ["lambda"]})
    assert changed.contract_sha != c.contract_sha


def test_engine_table() -> None:
    t = engine_table()
    assert t.max_package_bytes == 10_000_000_000
    assert t.lambda_.timeout_s == 900
    assert t.lambda_.safety_factor == 2
    assert set(t.delivery_bytes_per_s) == {"https", "s3", "sftp"}


# ---------------------------------------------------------------- dynamic models (portal responses, messages)


def _record(fields: dict[str, Any], unknown: str = "ignore") -> RecordSpec:
    return RecordSpec.model_validate({"unknown_fields": unknown, "fields": fields})


def test_record_model_validates_message() -> None:
    m = record_model(
        "package_ready",
        _record(
            {
                "study_id": {"type": "string", "pattern": "^[A-Z]{3}[0-9]{3,5}$"},
                "file_count": {"type": "integer", "minimum": 1},
                "schema": {"type": "string", "required": False},  # would shadow BaseModel.schema without aliases
                "slot": {"type": "enum", "values": ["x", "y"]},
                "targets": {
                    "type": "list",
                    "max_items": 2,
                    "items": {"type": "object", "fields": {"uri": {"type": "string"}}},
                },
            }
        ),
    )
    ok = m.model_validate(
        {"study_id": "ONC101", "file_count": 3, "slot": "x", "targets": [{"uri": "s3://b/k"}], "extra": 1}
    )
    assert ok.model_dump(by_alias=True)["study_id"] == "ONC101"
    for bad in (
        {"study_id": "onc", "file_count": 3, "slot": "x", "targets": []},
        {"study_id": "ONC101", "file_count": "3", "slot": "x", "targets": []},  # strict: no str -> int coercion
        {"study_id": "ONC101", "file_count": 0, "slot": "x", "targets": []},
        {"study_id": "ONC101", "file_count": 3, "slot": "z", "targets": []},
        {"study_id": "ONC101", "file_count": 3, "slot": "x", "targets": [{}, {}, {}]},
        {"file_count": 3, "slot": "x", "targets": []},
    ):
        with pytest.raises(ValidationError):
            m.model_validate(bad)


def test_record_model_reject_unknown_and_python_regex() -> None:
    m = record_model("strict_msg", _record({"id": {"type": "string", "pattern": r"^(?!tmp)[a-z]+$"}}, "reject"))
    assert m.model_validate({"id": "abc"})
    with pytest.raises(ValidationError):
        m.model_validate({"id": "tmpx"})  # lookahead works: python-re engine, like the Regex field type
    with pytest.raises(ValidationError):
        m.model_validate({"id": "abc", "other": 1})


def test_dynamic_models_are_deterministic() -> None:
    spec = {"b": FieldSpec(type="string"), "a": FieldSpec(type="integer")}
    s1 = build_model("msg", spec).model_json_schema()
    s2 = build_model("msg", dict(reversed(list(spec.items())))).model_json_schema()
    assert s1 == s2


def test_field_spec_shape_rules() -> None:
    for bad in (
        {"type": "enum"},
        {"type": "list"},
        {"type": "object"},
        {"type": "integer", "pattern": "^1$"},
        {"type": "string", "pattern": "("},
    ):
        with pytest.raises(ValidationError):
            FieldSpec.model_validate(bad)
    deep: dict[str, Any] = {"type": "string"}
    for _ in range(5):
        deep = {"type": "object", "fields": {"x": deep}}
    with pytest.raises(ValidationError, match="4 levels"):
        _record({"x": deep})


# ---------------------------------------------------------------- units and money


def test_units_are_integers_and_reject_bare_numbers() -> None:
    w = Workload.model_validate(
        {
            "bytes_per_run": {"p50": "1GB", "p95": "6GB", "max": "10GB"},
            "files_per_run": {"p50": 1, "p95": 2, "max": 3},
            "runs_per_day": 1,
        }
    )
    assert w.bytes_per_run.max == 10**10
    with pytest.raises(ValidationError):
        Workload.model_validate(
            {
                "bytes_per_run": {"p50": 1, "p95": 2, "max": 3},
                "files_per_run": {"p50": 1, "p95": 2, "max": 3},
                "runs_per_day": 1,
            }
        )
    with pytest.raises(ValidationError):  # p95 above max
        Workload.model_validate(
            {
                "bytes_per_run": {"p50": "1GB", "p95": "9GB", "max": "8GB"},
                "files_per_run": {"p50": 1, "p95": 2, "max": 3},
                "runs_per_day": 1,
            }
        )


def test_money_is_two_decimal_text() -> None:
    c = Cost.model_validate({"budget_per_run_usd": "2", "budget_per_month_usd": "300.005"})
    assert c.budget_per_run_usd == Decimal("2.00")
    assert c.model_dump(mode="json") == {"budget_per_run_usd": "2.00", "budget_per_month_usd": "300.01"}
    with pytest.raises(ValidationError):
        Cost.model_validate({"budget_per_run_usd": 2.5, "budget_per_month_usd": "1"})  # floats are refused


# ---------------------------------------------------------------- YAML round-trip and the BA view


def _spec_files() -> list[Path]:
    return sorted([*(EXAMPLE / "spec").glob("*.yaml"), *STARTERS.glob("*/spec/*.yaml")])


@pytest.mark.parametrize("path", _spec_files(), ids=lambda p: f"{p.parent.parent.name}/{p.name}")
def test_yaml_round_trip_is_byte_identical(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert yamlio.dump_text(yamlio.load_text(text)) == text


def test_ba_view_is_plain_and_deterministic() -> None:
    spec = load_spec(EXAMPLE).spec
    assert spec is not None
    text = render_ba(spec)
    assert text == render_ba(spec)
    assert text.startswith("# Study package delivery (study_pkg_delivery, version 0.1.0)")
    for t in spec.pipeline.orchestration.tasks:
        assert t.label in text
    for r in spec.rules.rules:
        assert " ".join(r.plain_english.split()) in " ".join(text.split())
    assert "clinops/{env}/" not in text  # secret paths are not shown to the BA
    assert "<REQUIRED>" not in text
