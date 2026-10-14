"""Rule Implementation → gates → Rule Review (separate context), with bounded self-correction.

The Test agent adds edge cases once (the owner's cases stay the oracle). A built-in op is preferred; custom code is
written to rules/<rule_id>/, gated, then reviewed. The outcome waits for a person (`factory accept`); agents never
accept.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory_core.envelope import CommandError
from factory_spec.catalog import op_catalog
from factory_spec.load import load_spec
from factory_spec.patch import SpecPatch, apply_patch

from .agents import IMPLEMENT, REVIEW, TESTS, ReviewAnswer
from .audit import AuditTrail
from .boundary import Part, build_prompt
from .errors import AgentError, Escalation
from .gates import EDGE_CASES, GateReport, run_gates
from .gates import REVIEW as REVIEW_FILE
from .loop import correct
from .runner import AgentRunner


@dataclass
class RuleOutcome:
    rule_id: str
    status: str = "escalated"  # builtin_applied | awaiting_acceptance | escalated
    gates: GateReport | None = None
    review: ReviewAnswer | None = None
    edge_case_questions: list[str] = field(default_factory=list)
    escalation: Escalation | None = None


def _plugin_api_doc() -> str:
    import factory_runtime.plugin_api as api

    return str(api.__doc__ or "")


def _rule_view(pipeline_dir: Path, rule_id: str) -> tuple[Any, dict[str, Any]]:
    spec = load_spec(pipeline_dir).spec
    if spec is None or spec.rule(rule_id) is None:
        raise AgentError("rule_unknown", f"rule {rule_id} is not in a loadable spec")
    rule: Any = spec.rule(rule_id)
    view = {"rule": rule.model_dump(mode="json", exclude={"implementation"}),
            "implementation": rule.implementation.model_dump(mode="json")}  # fmt: skip
    return rule, view


def _catalog() -> list[dict[str, str]]:
    return [{"op": c.op, "summary": c.summary} for c in op_catalog().values() if c.status == "available"]


def _edge_cases(runner: AgentRunner, pipeline_dir: Path, rule_id: str, audit: AuditTrail) -> list[str]:
    rule, view = _rule_view(pipeline_dir, rule_id)
    prompt, _ = build_prompt([Part("instructions", "Propose edge cases for this rule."), Part("spec", view, "Rule")])
    ans, prov = runner.run(TESTS, prompt)
    owner = {c.name for c in rule.test_cases}
    cases, seen = [], set()
    for c in ans.cases:
        if c.name in owner or c.name in seen:
            continue  # never shadow or duplicate an owner case
        try:
            given, expect = json.loads(c.given_json), json.loads(c.expect_json)
        except json.JSONDecodeError:
            continue
        if isinstance(given, dict) and isinstance(expect, dict):
            seen.add(c.name)
            cases.append({"name": c.name, "given": given, "expect": expect, "why": c.why})
    d = pipeline_dir / "rules" / rule_id
    d.mkdir(parents=True, exist_ok=True)
    (d / EDGE_CASES).write_text(json.dumps(cases, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    audit.record("test_generation", prov, attempt=1, verdict="ok", rule_id=rule_id, cases=len(cases))
    return list(ans.questions)


def _set_impl(pipeline_dir: Path, rule_id: str, impl: dict[str, Any]) -> list[str]:
    try:
        apply_patch(pipeline_dir, SpecPatch("rules.yaml", "set", f"rules[{rule_id}].implementation", impl))
    except CommandError as exc:
        return [f"rules.yaml: {i.code} at {i.loc}" for i in exc.issues] or [str(exc)]
    return []


def implement_rule(runner: AgentRunner, pipeline_dir: Path, rule_id: str, audit: AuditTrail, *,
                   tools: bool = True) -> RuleOutcome:  # fmt: skip
    out = RuleOutcome(rule_id)
    out.edge_case_questions = _edge_cases(runner, pipeline_dir, rule_id, audit)
    d = pipeline_dir / "rules" / rule_id

    def attempt(feedback: list[str], n: int) -> tuple[str, list[str], float]:
        rule, view = _rule_view(pipeline_dir, rule_id)
        parts = [Part("instructions", "Implement this rule: prefer a built-in op, else custom code + tests."),
                 Part("spec", view, "Rule"), Part("catalog", _catalog(), "Built-in ops"),
                 Part("spec", _plugin_api_doc(), "Plugin API")]  # fmt: skip
        if feedback:
            parts.append(Part("validator", feedback, "Fix these issues"))
        prompt, _ = build_prompt(parts)
        ans, prov = runner.run(IMPLEMENT, prompt)
        cost = float(prov.cost_usd or 0.0)
        if ans.builtin_op:
            c = op_catalog().get(ans.builtin_op)
            if c is None or c.status != "available":
                issues = [f"{ans.builtin_op} is not an available built-in op"]
            else:
                try:
                    params = json.loads(ans.params_json or "{}")
                except json.JSONDecodeError:
                    params = None
                issues = ["params_json is not a JSON object"] if not isinstance(params, dict) else _set_impl(
                    pipeline_dir, rule_id, {"kind": "builtin", "op": ans.builtin_op, "params": params})  # fmt: skip
            audit.record("rule_implementation", prov, attempt=n, verdict="builtin" if not issues else "issues",
                         rule_id=rule_id, op=ans.builtin_op, issues=len(issues))  # fmt: skip
            out.status = "builtin_applied" if not issues else out.status
            return "builtin", issues, cost
        d.mkdir(parents=True, exist_ok=True)
        (d / "impl.py").write_text(ans.code, encoding="utf-8")
        (d / "test_impl.py").write_text(ans.tests, encoding="utf-8")
        sha = hashlib.sha256(ans.code.encode()).hexdigest()
        kinds = rule.implementation.output_kinds if rule.implementation.kind == "custom" else {}
        kinds = dict(kinds) or dict.fromkeys(rule.outputs, "value")
        issues = _set_impl(pipeline_dir, rule_id, {"kind": "custom", "code_sha256": sha, "output_kinds": kinds})
        rule, view = _rule_view(pipeline_dir, rule_id)
        gates = run_gates(pipeline_dir, rule, tools=tools)
        out.gates = gates
        issues += gates.issues()
        audit.record("rule_implementation", prov, attempt=n, verdict="custom" if not issues else "issues",
                     rule_id=rule_id, code_sha256=sha, gates_ok=gates.ok, issues=len(issues))  # fmt: skip
        if issues:
            return "custom", issues, cost
        review, rprov = _review(runner, view, ans.code, ans.tests, gates)
        out.review = review
        record = {"verdict": review.verdict, "code_sha256": gates.code_sha256, "tests_sha256": gates.tests_sha256,
                  "findings": [f.model_dump() for f in review.findings], "summary": review.summary,
                  "output_sha256": rprov.output_sha256}  # fmt: skip
        (d / REVIEW_FILE).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        audit.record("rule_review", rprov, attempt=n, verdict=review.verdict, rule_id=rule_id,
                     findings=len(review.findings))  # fmt: skip
        if review.verdict != "approve":
            issues = [f"review {f.severity} ({f.category}): {f.message}" for f in review.findings
                      if f.severity in ("blocker", "major")] or [f"review rejected: {review.summary}"]  # fmt: skip
        else:
            out.status = "awaiting_acceptance"
        return "custom", issues, cost + float(rprov.cost_usd or 0.0)

    res = correct("rule_implementation", attempt, budget_usd=(IMPLEMENT.budget_usd + REVIEW.budget_usd) * 3,
                  action=f"Implement or simplify rule {rule_id} by hand, or restate its plain English.")  # fmt: skip
    out.escalation = res.escalation
    if res.escalation is not None:
        out.status = "escalated"
    return out


def _review(runner: AgentRunner, view: dict[str, Any], code: str, tests: str, gates: GateReport) -> Any:
    prompt, _ = build_prompt([
        Part("instructions", "Review this custom rule against its plain English and the checklist."),
        Part("spec", view, "Rule"), Part("spec", code, "impl.py"), Part("spec", tests, "test_impl.py"),
        Part("validator", gates.as_dict(), "Gate results"),
    ])  # fmt: skip
    return runner.run(REVIEW, prompt)
