"""The runtime command line of one task, from the DAG's runtime payload (pure: no Airflow import)."""

from __future__ import annotations

from typing import Any

FLAGS = (("--bundle", "bundle"), ("--sha256", "sha256"), ("--task", "task"), ("--run-key", "run_key"),
         ("--attempt", "attempt"), ("--run-id", "run_id"))  # fmt: skip


def runtime_argv(runtime: dict[str, Any]) -> list[str]:
    argv = [part for flag, key in FLAGS for part in (flag, str(runtime[key]))]
    if runtime.get("conf"):
        argv += ["--conf", str(runtime["conf"])]
    return argv
