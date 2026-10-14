"""The engine planner: deterministic code, not an LLM (ADR-0005, engine-selection.md §2).

Per task: candidates (intersection of the ops' allowed engines) → policy filters (recorded) → estimates (p95, max) →
feasibility → the lowest monthly cost; ties break lambda < mwaa < eks_pod_spot < eks_pod. A task made only of
fivetran.sync goes to fivetran_native. Explicit engines win when feasible, otherwise they are an error. Decisions are
printed (`factory plan`), never written to the lock (P3-8). Prices: the catalog placeholders unless
the platform supplies calibrated ones (`prices`).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from factory_spec.catalog import Pricing, engine_table, op_catalog, pricing
from factory_spec.estimate import allowed_engines, estimate_seconds
from factory_spec.spec import PipelineSpec

ORDER = ("lambda", "mwaa", "eks_pod_spot", "eks_pod", "fivetran_native", "fivetran_sdk")
CONTROL_EFFECTS = frozenset({"triggers_external_sync", "allocates_sequence"})
CENT = Decimal("0.0001")


def _ops(rules: list[Any]) -> list[Any]:
    return [op_catalog().get(r.implementation.op) for r in rules if r.implementation.kind == "builtin"]


def plan_task(spec: PipelineSpec, task: Any, prices: Pricing | None = None) -> dict[str, Any]:
    pr = prices or pricing()
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
    size = pr.sizing["data" if data else "control"]
    region = next((r for r in spec.security.security_and_access.residency.allowed_regions if r in pr.regions),
                  pr.default_region)  # fmt: skip
    rp = pr.regions[region]
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
            unit = Decimal(p95) * Decimal(size.lambda_memory_mb) / 1024 * Decimal(rp.lambda_gb_second)
            unit += Decimal(rp.lambda_request)
        elif e in ("eks_pod", "eks_pod_spot"):
            per_h = Decimal(size.pod_vcpu) * Decimal(rp.pod_vcpu_hour) + Decimal(size.pod_memory_gb) * Decimal(
                rp.pod_gb_hour
            )
            unit = Decimal(p95) * per_h / 3600 / Decimal(pr.pod_utilisation)
            if e == "eks_pod_spot":
                unit *= 1 - Decimal(pr.spot_discount)
        else:  # mwaa, fivetran_*: fixed or external cost; nothing at the margin
            unit = Decimal(pr.mwaa_task_marginal)
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


def plan(spec: PipelineSpec, prices: Pricing | None = None) -> dict[str, Any]:
    pr = prices or pricing()
    return {
        "pipeline_id": spec.pipeline_id,
        "pricing": f"{pr.version} ({pr.status})",
        "engine_decisions": [plan_task(spec, t, pr) for t in spec.pipeline.orchestration.tasks],
    }
