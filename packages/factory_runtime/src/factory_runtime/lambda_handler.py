"""Lambda entry point for task execution: the same contract as `python -m factory_runtime.run`.

Event: {"bundle": "s3://...", "sha256": "...", "task": "...", "run_key": "...", "attempt": 1, "run_id": "...",
        "conf": {...}?}
Returns (never raises): {"exit_code": int, "error_class": str | None, "status": str}. The MWAA operator maps the exit
code exactly like a pod's (factory_airflow, Phase 5). Warm invocations reset the bound log fields and clean the task
working folders; verified bundles stay cached by sha256.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any

from factory_core.errors import ErrorClass
from factory_core.jsonlog import configure

from .run import main

_REVERSE = {
    75: ErrorClass.TRANSIENT,
    65: ErrorClass.DATA,
    78: ErrorClass.CONFIG,
    77: ErrorClass.SECURITY,
    70: ErrorClass.INTERNAL,
}
_configured = False


def _argv(event: dict[str, Any]) -> list[str]:
    import json

    argv = [
        "--bundle", str(event.get("bundle", "")),
        "--sha256", str(event.get("sha256", "")),
        "--task", str(event.get("task", "")),
        "--run-key", str(event.get("run_key", "")),
        "--attempt", str(int(event.get("attempt", 1))),
        "--run-id", str(event.get("run_id", "")),
    ]  # fmt: skip
    if event.get("conf") is not None:
        argv += ["--conf", json.dumps(event["conf"])]
    return argv


def handle(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    global _configured
    if not _configured:
        configure(sys.stdout, service="factory-runtime-lambda")
        _configured = True
    runs = os.path.join(os.environ.get("FACTORY_WORKDIR", "/tmp/factory"), "runs")  # noqa: S108
    shutil.rmtree(runs, ignore_errors=True)
    code = main(_argv(event))
    shutil.rmtree(runs, ignore_errors=True)
    cls = _REVERSE.get(code)
    return {"exit_code": code, "error_class": str(cls) if cls else None, "status": "ok" if code == 0 else "failed"}
