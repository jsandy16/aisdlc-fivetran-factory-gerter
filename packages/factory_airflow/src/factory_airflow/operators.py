"""Operators used by generated DAGs (Airflow 3.3.1 on MWAA). Control only: no data, no secrets.

Exit-code contract of the runtime: 0 ok · 75 transient (retried) · 65 data · 77 security · 78 config · 70 internal
(failed at once: retrying cannot fix them). The runtime receives ids only: bundle, sha, task, run key, attempt, run id.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.sdk import BaseOperator, BaseSensorOperator
from airflow.sdk.exceptions import AirflowException, AirflowFailException

TRANSIENT = 75
EXIT_MEANING = {65: "data", 70: "internal", 75: "transient", 77: "security", 78: "config"}


def _invoke(function_name: str, runtime: dict[str, Any], hook: Any = None) -> dict[str, Any]:
    if hook is None:
        from airflow.providers.amazon.aws.hooks.lambda_function import LambdaHook

        hook = LambdaHook()
    res = hook.invoke_lambda(function_name=function_name, invocation_type="RequestResponse",
                             payload=json.dumps(runtime, sort_keys=True))  # fmt: skip
    if res.get("FunctionError"):
        raise AirflowException(f"{function_name} failed to run ({res['FunctionError']})")
    payload: dict[str, Any] = json.loads(res["Payload"].read())
    return payload


def _raise_for(code: int, what: str) -> None:
    meaning = EXIT_MEANING.get(code, "unknown")
    if code == TRANSIENT:
        raise AirflowException(f"{what} ended transient (75); Airflow retries it")
    raise AirflowFailException(f"{what} ended {meaning} ({code}); not retried")


class FactoryLambdaOperator(BaseOperator):
    template_fields: Sequence[str] = ("runtime",)

    def __init__(self, *, function_name: str, runtime: dict[str, Any], hook: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.function_name = function_name
        self.runtime = runtime
        self.hook = hook

    def execute(self, context: Any) -> dict[str, Any]:
        payload = _invoke(self.function_name, self.runtime, self.hook)
        code = int(payload.get("exit_code", 70))
        if code != 0:
            _raise_for(code, self.task_id)
        return {"exit_code": 0, "status": payload.get("status", "ok")}


class FactoryPollSensor(BaseSensorOperator):
    """Each poke invokes the runtime Lambda (poll budget per invocation); 75 means "not finished yet"."""

    template_fields: Sequence[str] = ("runtime",)

    def __init__(self, *, function_name: str, runtime: dict[str, Any], hook: Any = None, **kwargs: Any) -> None:
        kwargs.setdefault("mode", "reschedule")
        super().__init__(**kwargs)
        self.function_name = function_name
        self.runtime = runtime
        self.hook = hook

    def poke(self, context: Any) -> bool:
        code = int(_invoke(self.function_name, self.runtime, self.hook).get("exit_code", 70))
        if code == 0:
            return True
        if code == TRANSIENT:
            return False
        _raise_for(code, self.task_id)
        return False  # pragma: no cover


def runtime_argv(runtime: dict[str, Any]) -> list[str]:
    keys = (("--bundle", "bundle"), ("--sha256", "sha256"), ("--task", "task"), ("--run-key", "run_key"),
            ("--attempt", "attempt"), ("--run-id", "run_id"))  # fmt: skip
    argv = [part for flag, key in keys for part in (flag, runtime[key])]
    if runtime.get("conf"):
        argv += ["--conf", runtime["conf"]]
    return argv


class FactoryPodOperator(KubernetesPodOperator):
    """`python -m factory_runtime.run` in the team namespace with the task's service account (IRSA)."""

    def __init__(self, *, runtime: dict[str, Any], cpu: str, memory: str, spot: bool = False, **kwargs: Any) -> None:
        from kubernetes.client import models as k8s

        resources = k8s.V1ResourceRequirements(requests={"cpu": cpu, "memory": memory},
                                               limits={"cpu": cpu, "memory": memory})  # fmt: skip
        if spot:
            kwargs["node_selector"] = {"eks.amazonaws.com/capacityType": "SPOT"}
            kwargs["tolerations"] = [k8s.V1Toleration(key="spot", operator="Exists", effect="NoSchedule")]
        super().__init__(
            name=kwargs["task_id"].replace("_", "-"),
            cmds=["python", "-m", "factory_runtime.run"],
            arguments=runtime_argv(runtime),
            container_resources=resources,
            on_finish_action="delete_pod",
            get_logs=True,
            **kwargs,
        )
