"""`factory cost`: the planner's unit costs × runs per month, plus the pipeline's share of MWAA, against the budget.

Prices are placeholders (pricing.v1.json, status "placeholder") until calibrated in Phase 6/8; the report says so.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from factory_spec.catalog import pricing
from factory_spec.spec import PipelineSpec

from ..plan.engine import plan

CENT = Decimal("0.01")


def cost_report(spec: PipelineSpec) -> dict[str, Any]:
    p = spec.pipeline
    runs = Decimal(p.workload.runs_per_day * 30)
    tasks = []
    month = Decimal(0)
    for d in plan(spec)["engine_decisions"]:
        engine = d["choice"]
        monthly = Decimal(d["monthly_cost_usd"].get(engine, "0")) if engine else Decimal(0)
        month += monthly
        tasks.append({"task_id": d["task_id"], "engine": engine, "monthly_usd": str(monthly.quantize(CENT))})
    mwaa = Decimal(pricing().mwaa_share_per_pipeline_month)
    total = month + mwaa
    per_run = (month / runs) if runs else Decimal(0)
    budget_month = Decimal(str(p.cost.budget_per_month_usd))
    budget_run = Decimal(str(p.cost.budget_per_run_usd))
    return {
        "pricing": f"{pricing().version} ({pricing().status})",
        "runs_per_month": int(runs),
        "tasks": tasks,
        "mwaa_share_usd": str(mwaa.quantize(CENT)),
        "monthly_usd": str(total.quantize(CENT)),
        "per_run_usd": str(per_run.quantize(Decimal("0.0001"))),
        "budget_per_month_usd": str(budget_month),
        "budget_per_run_usd": str(budget_run),
        "within_budget": total <= budget_month and per_run <= budget_run,
    }
