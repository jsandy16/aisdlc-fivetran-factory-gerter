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


def pod_exit_code(remote_pod: Any, container: str) -> int | None:
    """The runtime container's exit code from a finished pod (None if it never terminated: eviction, image pull)."""
    status = getattr(remote_pod, "status", None)
    for cs in getattr(status, "container_statuses", None) or []:
        if cs.name == container:
            term = getattr(getattr(cs, "state", None), "terminated", None)
            return int(term.exit_code) if term is not None and term.exit_code is not None else None
    return None
