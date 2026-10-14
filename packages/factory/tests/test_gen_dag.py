"""DAG generator: deterministic source, parse-time rules (no top-level I/O, allow-listed imports), graph = spec."""

from __future__ import annotations

import ast
from pathlib import Path

from factory.gen.dag import generate_dag
from factory.gen.names import Deploy
from factory_spec.load import load_spec

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "team-pipelines" / "pipelines" / "study_pkg_delivery"
FIVETRAN = ROOT / "packages" / "factory_runtime" / "tests" / "data" / "unit_repo" / "pipelines" / "unit_fivetran"
ALLOWED_IMPORTS = {"datetime", "airflow.sdk", "factory_airflow.operators"}


def _dag(pdir: Path) -> str:
    spec = load_spec(pdir).spec
    assert spec is not None
    return generate_dag(
        spec, Deploy.placeholder("dev", "clinops_data", "eu-west-1", spec.pipeline_id, "b" * 64), "c" * 64
    )


def test_generated_dag_is_deterministic_and_safe_at_parse_time() -> None:
    src = _dag(EXAMPLE)
    assert src == _dag(EXAMPLE)
    tree = ast.parse(src)
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= ALLOWED_IMPORTS
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    top_calls = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    assert top_calls == []  # nothing runs at import except building the DAG object
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"open", "boto3", "requests", "os", "subprocess", "Variable", "Connection"}


def test_graph_engines_and_ids_only() -> None:
    src = _dag(EXAMPLE)
    assert "t1_collect = FactoryLambdaOperator(" in src
    assert "t2_checksum = FactoryLambdaOperator(" in src
    assert "t3_upload = FactoryPodOperator(" in src  # the planner's choice at 10 GB
    assert "t1_collect >> t2_checksum" in src
    assert "t2_checksum >> t3_upload" in src
    assert "spot=False" in src  # GxP: never spot
    assert "ONC101" not in src
    assert "acme-clinops-landing" not in src


def test_fivetran_task_is_a_rescheduling_poll_sensor() -> None:
    src = _dag(FIVETRAN)
    assert "t1_sync = FactoryPollSensor(" in src
    assert "schedule='0 */6 * * *'" in src
    assert '"conf": "{\\"data_interval_start\\": \\"{{ data_interval_start }}\\"}"' in src
