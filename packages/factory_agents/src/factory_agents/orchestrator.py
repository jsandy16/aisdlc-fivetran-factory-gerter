"""The Phase 4 orchestrator: requirement → spec → custom rules → gates → review → (person) acceptance → unit tests,
then a slim review package (agents-and-gates.md §4; generation, IaC, integration tests and cost follow in Phase 5).

It stops, with a clear next step, wherever a person is needed: open questions for the business owner, an
escalation, or custom rules awaiting `factory accept`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory_spec.acceptance import acceptance_problem
from factory_spec.ba_view import render_ba
from factory_spec.load import load_spec
from factory_spec.owner_tests import run_owner_tests
from factory_spec.validate import validate_dir

from .audit import AuditTrail
from .errors import AgentError
from .gates import run_gates
from .intake import intake
from .rules import implement_rule
from .runner import AgentRunner


@dataclass
class RunReport:
    stage: str  # needs_answers | escalated | awaiting_acceptance | ready
    next_step: str
    details: dict[str, Any] = field(default_factory=dict)


def orchestrate(runner: AgentRunner, pipeline_dir: Path, requirement: str | None, *, tools: bool = True) -> RunReport:
    audit = AuditTrail(pipeline_dir)
    if requirement is not None:
        res = intake(runner, pipeline_dir, requirement, audit)
        if res.escalation:
            return RunReport("escalated", res.escalation.recommended_human_action,
                             {"escalation": res.escalation.model_dump()})  # fmt: skip
        if res.open_questions:
            return RunReport("needs_answers", "The business owner answers the open questions; then run again.",
                             {"open_questions": [q.model_dump() for q in res.open_questions]})  # fmt: skip
    spec = load_spec(pipeline_dir).spec
    if spec is None:
        return RunReport("escalated", "The spec does not load; fix it by hand.")
    waiting = []
    for r in spec.rules.rules:
        if r.implementation.kind != "custom":
            continue
        d = pipeline_dir / "rules" / r.rule_id
        if acceptance_problem(r, d) is None:
            continue
        if not (d / "review.json").is_file():
            out = implement_rule(runner, pipeline_dir, r.rule_id, audit, tools=tools)
            if out.escalation:
                return RunReport("escalated", out.escalation.recommended_human_action,
                                 {"rule_id": r.rule_id, "escalation": out.escalation.model_dump()})  # fmt: skip
        waiting.append(r.rule_id)
    if waiting:
        step = "A person reviews and runs `factory accept <pipeline> <rule_id> --by <name>`."
        return RunReport("awaiting_acceptance", step, {"rules": waiting})
    return RunReport("ready", "Phase 5 generates, provisions and integration-tests the pipeline.",
                     write_review_package(pipeline_dir, tools=tools))  # fmt: skip


def write_review_package(pipeline_dir: Path, *, tools: bool = True) -> dict[str, Any]:
    """build/review/<pipeline>/: deterministic spec view, validation, unit tests, gates, reviews, acceptances."""
    res = validate_dir(pipeline_dir)
    spec = res.load.spec
    if spec is None:
        raise AgentError("spec_invalid", "the spec does not load")
    cases = run_owner_tests(spec)
    rules = {}
    for r in spec.rules.rules:
        if r.implementation.kind == "custom":
            d = pipeline_dir / "rules" / r.rule_id
            lock = json.loads((d / "impl.lock.json").read_text(encoding="utf-8"))
            rules[r.rule_id] = {"gates": run_gates(pipeline_dir, r, tools=tools).as_dict(),
                                "accepted_by": lock["accepted_by"], "digest": lock["digest"]}  # fmt: skip
    summary = {
        "pipeline_id": spec.pipeline_id,
        "valid": not res.errors,
        "issues": [i.model_dump(mode="json") for i in res.issues],
        "owner_tests": [c.__dict__ for c in cases],
        "custom_rules": rules,
        "agent_decisions": len(AuditTrail(pipeline_dir).entries()),
    }
    out = pipeline_dir.parent.parent / "build" / "review" / spec.pipeline_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "review.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    (out / "spec.md").write_text(render_ba(spec), encoding="utf-8")
    return {"review_package": str(out), "valid": summary["valid"],
            "owner_tests_failed": sum(1 for c in cases if c.status == "fail")}  # fmt: skip
