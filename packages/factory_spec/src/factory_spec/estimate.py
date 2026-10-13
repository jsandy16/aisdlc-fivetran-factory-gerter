"""Run-time estimates per task and engine, shared by the validator (Lambda feasibility) and the engine planner.

Consecutive streaming ops are fused into one segment whose speed is its slowest stage; every op adds its overhead.
Delivery speed uses the worst destination type allowed in *any* environment, so the engine choice is the same in every
environment. Constants come from the catalog (`resources`) and `engine_table.v1.json`; they are placeholders until
calibrated in Phase 8 (spike S6).
"""

from __future__ import annotations

from typing import Any

from .catalog import engine_table, op_catalog
from .spec import PipelineSpec

ALL_ENGINES = ("lambda", "eks_pod", "eks_pod_spot", "mwaa", "fivetran_native", "fivetran_sdk")
BUNDLE_ENGINES = frozenset({"lambda", "eks_pod", "eks_pod_spot"})  # engines that run the runtime image and bundle


def allowed_engines(task_rules: list[Any]) -> set[str]:
    """Engines every rule of the task can run on: the catalog's allowed engines (spot wherever eks_pod is allowed);
    expressions and custom code need the runtime (Lambda or a pod)."""
    allowed = set(ALL_ENGINES)
    for r in task_rules:
        impl = r.implementation
        c = op_catalog().get(impl.op) if impl.kind == "builtin" else None
        if c is not None and c.status == "available":
            engines = set(c.allowed_engines)
            if "eks_pod" in engines:
                engines.add("eks_pod_spot")
            allowed &= engines
        else:
            allowed &= BUNDLE_ENGINES
    return allowed


def _delivery_bps(spec: PipelineSpec, slot_id: str) -> int:
    table = engine_table()
    slot = next((s for s in spec.integrations.destinations if s.id == slot_id), None)
    types = set(slot.allowed_types) if slot else {"sftp"}
    entry_types = {
        e.type for es in spec.destinations.destinations.values() for e in es if slot and slot.id in e.slots
    } or types
    return min(table.delivery_bytes_per_s[t] for t in entry_types)


def estimate_seconds(spec: PipelineSpec, task_rules: list[Any], engine: str, nbytes: int) -> int:
    """Seconds to run the rules on `engine` ("lambda" or "eks_pod") for a run of `nbytes`."""
    total = 0.0
    seg: float | None = None
    prev_streams = False
    for r in task_rules:
        impl = r.implementation
        c = op_catalog().get(impl.op) if impl.kind == "builtin" else None
        if c is None or c.status != "available":
            total += 1
            continue
        res = c.resources.get(engine)
        total += res.overhead_s if res else 5
        bps = res.bytes_per_s if res else None
        if impl.op == "destination.deliver":
            bps = _delivery_bps(spec, impl.params.slot)
        streams_in = bool(c.inputs) and c.inputs[0].streaming
        if bps:
            if seg is not None and prev_streams and streams_in:
                seg = min(seg, bps)
            else:
                if seg is not None:
                    total += nbytes / seg
                seg = bps
        else:
            if seg is not None:
                total += nbytes / seg
            seg = None
        prev_streams = bool(c.outputs) and c.outputs[0].streaming
    if seg is not None:
        total += nbytes / seg
    return round(total)


def estimate_lambda_seconds(spec: PipelineSpec, task_rules: list[Any]) -> int:
    """Seconds for the task on Lambda at workload.max (the validator's feasibility input)."""
    return estimate_seconds(spec, task_rules, "lambda", spec.pipeline.workload.bytes_per_run.max)
