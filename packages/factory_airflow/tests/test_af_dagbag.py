"""Generated DAGs load in a real Airflow 3.3.1 (DagBag), parse in < 1 s, and the operators map exit codes.

Opt-in: `FACTORY_AIRFLOW_PYTHON=<venv>/bin/python pytest -m airflow` (Airflow is not a dependency of the workspace).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from factory.gen.dag import generate_dag
from factory.gen.names import Deploy
from factory_spec.load import load_spec

pytestmark = [
    pytest.mark.airflow,
    pytest.mark.skipif(not os.environ.get("FACTORY_AIRFLOW_PYTHON"), reason="set FACTORY_AIRFLOW_PYTHON"),
]
ROOT = Path(__file__).resolve().parents[3]
PIPELINES = {
    "study_pkg_delivery": ROOT / "examples" / "team-pipelines" / "pipelines" / "study_pkg_delivery",
    "unit_fivetran": ROOT
    / "packages"
    / "factory_runtime"
    / "tests"
    / "data"
    / "unit_repo"
    / "pipelines"
    / "unit_fivetran",
}
PROBE = r"""
import io, json, sys, time
from airflow.dag_processing.dagbag import DagBag
from airflow.sdk.exceptions import AirflowException, AirflowFailException
from factory_airflow.operators import FactoryLambdaOperator, FactoryPollSensor
t0 = time.monotonic()
bag = DagBag(dag_folder=sys.argv[1])
parse_s = time.monotonic() - t0
out = {"errors": {k: v[:300] for k, v in bag.import_errors.items()}, "parse_s": parse_s, "dags": {}}
for dag_id, dag in bag.dags.items():
    out["dags"][dag_id] = {t.task_id: {"type": type(t).__name__, "down": sorted(t.downstream_task_ids),
                                       "retries": t.retries} for t in dag.tasks}
class Hook:
    def __init__(self, code): self.code = code
    def invoke_lambda(self, **kw):
        return {"Payload": io.BytesIO(json.dumps({"exit_code": self.code, "status": "x"}).encode())}
res = {}
for code in (0, 75, 65, 77, 78):
    op = FactoryLambdaOperator(task_id=f"t{code}", function_name="f", runtime={}, hook=Hook(code))
    try:
        op.execute({}); res[code] = "ok"
    except AirflowFailException: res[code] = "fail"
    except AirflowException: res[code] = "retry"
out["lambda"] = res
from kubernetes.client import models as k8s
from factory_airflow.operators import FactoryPodOperator
rt = {"bundle": "s3://b/x.zip", "sha256": "d" * 64, "task": "t", "run_key": "k", "attempt": 1, "run_id": "r"}
pods = {}
for code in (75, 65, 77, 78, None):
    op = FactoryPodOperator(task_id="t3", runtime=rt, cpu="1", memory="1Gi", image="x", namespace="n")
    op.process_pod_deletion = lambda *a, **k: True
    op.patch_already_checked = lambda *a, **k: None
    op.is_istio_enabled = lambda *a, **k: False  # no cluster here: the real check reads the pod
    term = k8s.V1ContainerState(terminated=k8s.V1ContainerStateTerminated(exit_code=code)) if code else None
    st = k8s.V1ContainerStatus(name="base", state=term, image="x", image_id="x", ready=False, restart_count=0)
    status = k8s.V1PodStatus(phase="Failed", container_statuses=[st])
    pod = k8s.V1Pod(metadata=k8s.V1ObjectMeta(name="p"), status=status)
    try:
        op.cleanup(pod, pod); pods[str(code)] = "ok"
    except AirflowFailException: pods[str(code)] = "fail"
    except AirflowException: pods[str(code)] = "retry"
out["pod"] = pods
s = FactoryPollSensor(task_id="s", function_name="f", runtime={}, hook=Hook(75))
out["sensor_75"] = s.poke({}); out["sensor_mode"] = s.mode
print(json.dumps(out))
"""


def test_generated_dags_load_in_airflow(tmp_path: Path) -> None:
    for pid, pdir in PIPELINES.items():
        spec = load_spec(pdir).spec
        assert spec is not None
        deploy = Deploy.placeholder("dev", "clinops_data", "eu-west-1", pid, "d" * 64)
        (tmp_path / f"{pid}.py").write_text(generate_dag(spec, deploy, "e" * 64))
    env = {**os.environ, "AIRFLOW_HOME": str(tmp_path / "home"), "AIRFLOW__CORE__LOAD_EXAMPLES": "False"}
    proc = subprocess.run([os.environ["FACTORY_AIRFLOW_PYTHON"], "-c", PROBE, str(tmp_path)], env=env,
                          capture_output=True, text=True, timeout=300, check=True)  # fmt: skip
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["errors"] == {}
    assert out["parse_s"] < 1.0 * len(PIPELINES)
    assert out["dags"]["study_pkg_delivery"] == {
        "t1_collect": {"type": "FactoryLambdaOperator", "down": ["t2_checksum"], "retries": 2},
        "t2_checksum": {"type": "FactoryLambdaOperator", "down": ["t3_upload"], "retries": 2},
        "t3_upload": {"type": "FactoryPodOperator", "down": [], "retries": 2},
    }
    assert out["dags"]["unit_fivetran"]["t1_sync"]["type"] == "FactoryPollSensor"
    assert out["lambda"] == {"0": "ok", "75": "retry", "65": "fail", "77": "fail", "78": "fail"}
    assert (out["sensor_75"], out["sensor_mode"]) == (False, "reschedule")
    assert out["pod"] == {"75": "retry", "65": "fail", "77": "fail", "78": "fail", "None": "retry"}
