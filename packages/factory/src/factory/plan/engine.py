"""The engine planner: deterministic code, not an LLM (ADR-0005, engine-selection.md §2).

Per task: candidates (intersection of the ops' allowed engines) → policy filters (recorded) → estimates (p95, max) →
feasibility → the lowest monthly cost; ties break lambda < mwaa < eks_pod_spot < eks_pod. A task made only of
fivetran.sync goes to fivetran_native. Explicit engines win when feasible, otherwise they are an error. Decisions are
printed (`factory plan`), never written to the lock (P3-8). Prices are placeholders (catalog/pricing.v1.json).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from factory_spec.catalog import engine_table, op_catalog, pricing
from factory_spec.estimate import allowed_engines, estimate_seconds
from factory_spec.spec import PipelineSpec

ORDER = ("lambda", "mwaa", "eks_pod_spot", "eks_pod", "fivetran_native", "fivetran_sdk")
CONTROL_EFFECTS = frozenset({"triggers_external_sync", "allocates_sequence"})
CENT = Decimal("0.0001")


def _ops(rules: list[Any]) -> list[Any]:
    return [op_catalog().get(r.implementation.op) for r in rules if r.implementation.kind == "builtin"]


def plan_task(spec: PipelineSpec, task: Any) -> dict[str, Any]:
    rules = [spec.rule(x) for x in task.rules]
    ops = [c for c in _ops(rules) if c is not None]
    candidates = sorted(allowed_engines(rules), key=ORDER.index)
    filters: list[dict[str, str]] = []

    def drop(engine: str, rule: str) -> None:
        if engine in candidates:
            candidates.remove(engine)
            filters.append({"engine": engine, "rule": rule})

    if any(set(c.side_effects) - CONTROL_EFFECTS for c in ops) or len(ops) < len(rules):
        drop("mwaa", "mwaa_control_only")
    if spec.security.security_and_access.gxp_tier != "non_gxp":
        drop("eks_pod_spot", "gxp_excludes_spot")
    if any(c.determinism == "nondeterministic" for c in ops):
        drop("eks_pod_spot", "spot_needs_restartable_ops")

    wl = spec.pipeline.workload
    data = any(r.bytes_per_s for c in ops for r in c.resources.values())
    size = pricing().sizing["data" if data else "control"]
    region = next((r for r in spec.security.security_and_access.residency.allowed_regions if r in pricing().regions),
                  pricing().default_region)  # fmt: skip
    prices = pricing().regions[region]
    lam = engine_table().lambda_
    est: dict[str, dict[str, int]] = {}
    feasible: dict[str, str | None] = {}  # None = feasible, else why not
    cost: dict[str, str] = {}
    runs = Decimal(wl.runs_per_day * 30)
    for e in candidates:
        res_engine = "eks_pod" if e == "eks_pod_spot" else e
        p95 = estimate_seconds(spec, rules, res_engine, wl.bytes_per_run.p95)
        mx = estimate_seconds(spec, rules, res_engine, wl.bytes_per_run.max)
        est[e] = {"seconds_p95": p95, "seconds_max": mx}
        why = None
        if e == "lambda" and mx * lam.safety_factor > lam.timeout_s:
            why = f"{lam.safety_factor} x {mx} s at max > {lam.timeout_s} s"
        elif e == "eks_pod_spot" and p95 > 3600:
            why = f"{p95} s at p95 > 3600 s"
        feasible[e] = why
        if e == "lambda":
            unit = Decimal(p95) * Decimal(size.lambda_memory_mb) / 1024 * Decimal(prices.lambda_gb_second)
            unit += Decimal(prices.lambda_request)
        elif e in ("eks_pod", "eks_pod_spot"):
            per_h = Decimal(size.pod_vcpu) * Decimal(prices.pod_vcpu_hour) + Decimal(size.pod_memory_gb) * Decimal(
                prices.pod_gb_hour
            )
            unit = Decimal(p95) * per_h / 3600 / Decimal(pricing().pod_utilisation)
            if e == "eks_pod_spot":
                unit *= 1 - Decimal(pricing().spot_discount)
        else:  # mwaa, fivetran_*: fixed or external cost; nothing at the margin
            unit = Decimal(pricing().mwaa_task_marginal)
        cost[e] = str((unit * runs).quantize(CENT))

    ok = [e for e in candidates if feasible[e] is None]
    if task.engine != "auto":
        choice = task.engine if task.engine in ok else None
        reason = "explicit" if choice else (
            f"explicit '{task.engine}' is " + ("not allowed" if task.engine not in candidates else "infeasible")
        )  # fmt: skip
    elif ops and all(c.op == "fivetran.sync" for c in ops) and "fivetran_native" in ok:
        choice, reason = "fivetran_native", "tabular source: Fivetran runs the sync"
    elif ok:
        choice = min(ok, key=lambda e: (Decimal(cost[e]), ORDER.index(e)))
        reason = "lowest monthly cost among feasible engines"
    else:
        choice, reason = None, "no feasible engine"
    return {
        "task_id": task.task_id,
        "requested": task.engine,
        "candidates": candidates,
        "filters": filters,
        "estimates": est,
        "infeasible": {e: w for e, w in feasible.items() if w},
        "monthly_cost_usd": cost,
        "choice": choice,
        "reason": reason,
    }


def plan(spec: PipelineSpec) -> dict[str, Any]:
    return {
        "pipeline_id": spec.pipeline_id,
        "pricing": f"{pricing().version} ({pricing().status})",
        "engine_decisions": [plan_task(spec, t) for t in spec.pipeline.orchestration.tasks],
    }
