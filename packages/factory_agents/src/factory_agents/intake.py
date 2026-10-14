"""Intake: requirement → typed spec patches + open questions (agents-and-gates.md §4, Intake → SpecDraft → Validate).

Deterministic code decides what is applied:
- only allow-listed paths; governance, platform and infrastructure identities are never written by an agent
  (security.yaml, destinations.yaml, environments.yaml, secrets, team, owners, cost);
- every patch must quote the (redacted) requirement as evidence;
- each patch is applied with rollback if it adds validation errors;
- the spec is validated; remaining errors go back to the agent as structured feedback (at most 3 attempts).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory_core.envelope import CommandError
from factory_spec.files import SPEC_DIR, SPEC_FILES
from factory_spec.patch import SpecPatch, apply_patch
from factory_spec.validate import validate_dir

from .agents import INTAKE, IntakeAnswer, Question
from .audit import AuditTrail
from .boundary import Part, build_prompt, redact_secrets
from .errors import Escalation
from .loop import correct
from .runner import AgentRunner

ALLOWED: dict[str, tuple[str, ...]] = {
    "pipeline.yaml": ("metadata.name", "metadata.description", "metadata.requirements", "variables", "trigger",
                      "orchestration", "reliability", "workload", "observability.sla", "open_questions"),
    "rules.yaml": ("rules",),
    "integrations.yaml": ("sources", "portal", "destinations", "apis"),
}  # fmt: skip
INSTRUCTIONS = (
    "Propose spec patches for the requirement below. Allowed paths (file: path prefixes): {allowed}. "
    "Anything else is rejected. Quote the requirement as evidence for every patch."
)


def path_allowed(file: str, path: str) -> bool:
    if ".implementation" in path or path.endswith("implementation"):
        return False  # implementations come only through implement -> gates -> review -> a person's acceptance
    prefixes = ALLOWED.get(file, ())
    head = path.split("[", 1)[0]
    return any(head == p or head.startswith(p + ".") or path.startswith(p + "[") for p in prefixes)


@dataclass
class IntakeResult:
    applied: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[Question] = field(default_factory=list)
    escalation: Escalation | None = None
    valid: bool = False


def _spec_text(pipeline_dir: Path) -> dict[str, str]:
    return {f: (pipeline_dir / SPEC_DIR / f).read_text(encoding="utf-8") for f in sorted(SPEC_FILES)
            if (pipeline_dir / SPEC_DIR / f).is_file()}  # fmt: skip


def intake(runner: AgentRunner, pipeline_dir: Path, requirement: str, audit: AuditTrail) -> IntakeResult:
    redacted, _ = redact_secrets(requirement)
    out = IntakeResult()

    def attempt(feedback: list[str], n: int) -> tuple[IntakeAnswer, list[str], float]:
        parts = [
            Part("instructions", INSTRUCTIONS.format(allowed=json.dumps(ALLOWED, sort_keys=True))),
            Part("requirement", requirement, "Requirement"),
            Part("spec", _spec_text(pipeline_dir), "Current spec"),
        ]
        if feedback:
            parts.append(Part("validator", feedback, "Fix these issues"))
        prompt, _ = build_prompt(parts)
        ans, prov = runner.run(INTAKE, prompt)
        issues: list[str] = []
        applied = 0
        for p in ans.patches:
            where = f"{p.file}:{p.path}"
            if not path_allowed(p.file, p.path):
                issues.append(f"{where}: not an allowed path for the intake agent")
                continue
            if not p.evidence.strip() or p.evidence.strip() not in redacted:
                issues.append(f"{where}: the evidence is not a quote from the requirement")
                continue
            try:
                value = json.loads(p.value_json) if p.op != "remove" else None
                apply_patch(pipeline_dir, SpecPatch(p.file, p.op, p.path, value))
            except json.JSONDecodeError:
                issues.append(f"{where}: value_json is not JSON")
                continue
            except CommandError as exc:
                detail = "; ".join(f"{i.code} at {i.loc}" for i in exc.issues) or str(exc)
                issues.append(f"{where}: rejected ({detail})")
                continue
            applied += 1
            out.applied.append({"file": p.file, "op": p.op, "path": p.path})
        res = validate_dir(pipeline_dir)
        # custom-rule findings belong to implement -> gates -> review -> acceptance, not to intake
        issues += [f"{i.file}:{i.loc}: {i.code}: {i.msg}" for i in res.errors
                   if i.code != "required" and not i.code.startswith("custom_rule_")]  # fmt: skip
        audit.record("intake", prov, attempt=n, verdict="ok" if not issues else "issues", patches=len(ans.patches),
                     applied=applied, issues=len(issues), open_questions=len(ans.open_questions))  # fmt: skip
        out.open_questions = list(ans.open_questions)
        return ans, issues, float(prov.cost_usd or 0.0)

    res = correct("intake", attempt, budget_usd=INTAKE.budget_usd * 3,
                  action="Answer the open questions or edit the spec by hand, then run intake again.")  # fmt: skip
    out.escalation = res.escalation
    out.valid = res.escalation is None
    return out


def load_requirement(path: Path) -> str:
    """Requirement documents are read as text (Markdown or plain text)."""
    return path.read_text(encoding="utf-8")
