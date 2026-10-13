"""The deterministic engine planner (engine-selection.md §2) on the worked example and the Fivetran unit pipeline."""

from __future__ import annotations

from pathlib import Path

from factory.plan.engine import plan, plan_task
from factory_spec.load import load_spec

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "team-pipelines" / "pipelines" / "study_pkg_delivery"
FIVETRAN = ROOT / "packages" / "factory_runtime" / "tests" / "data" / "unit_repo" / "pipelines" / "unit_fivetran"


def test_worked_example_plan() -> None:
    spec = load_spec(EXAMPLE).spec
    assert spec is not None
    out = plan(spec)
    assert [(d["task_id"], d["choice"]) for d in out["engine_decisions"]] == [
        ("t1_collect", "lambda"),
        ("t2_checksum", "lambda"),
        ("t3_upload", "eks_pod"),
    ]
    t3 = out["engine_decisions"][2]
    assert "lambda" in t3["infeasible"]  # two SFTP uploads at 10 GB do not fit Lambda with the 2x margin
    assert {"engine": "eks_pod_spot", "rule": "gxp_excludes_spot"} in t3["filters"]
    assert plan(spec) == out  # deterministic


def test_explicit_infeasible_engine_is_reported() -> None:
    spec = load_spec(EXAMPLE).spec
    assert spec is not None
    t3 = next(t for t in spec.pipeline.orchestration.tasks if t.task_id == "t3_upload")
    d = plan_task(spec, t3.model_copy(update={"engine": "lambda"}))
    assert d["choice"] is None
    assert d["reason"] == "explicit 'lambda' is infeasible"


def test_fivetran_task_goes_to_fivetran_native() -> None:
    spec = load_spec(FIVETRAN).spec
    assert spec is not None
    t = spec.pipeline.orchestration.tasks[0]
    d = plan_task(spec, t.model_copy(update={"engine": "auto"}))
    assert d["choice"] == "fivetran_native"
    assert "lambda" not in d["candidates"]
