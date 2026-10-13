"""Artifact graph: which rule produces each artifact, who consumes it, its kind and durability.

Inputs bind to an op's input ports by kind (order does not matter); outputs bind to output ports by position.
Durability: `staged` when the artifact crosses a task boundary (or the op requires it), `stream` when producer and
consumers stream within one task, `final` for delivery receipts, otherwise `local`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from factory_core.envelope import Issue

from .catalog import op_catalog
from .codes import issue
from .spec import PipelineSpec

RULES = "rules.yaml"
TERMINAL_KINDS = frozenset({"receipt", "report"})
NO_CROSS_KINDS = frozenset({"file_set"})  # streams of many files: consumed in the task that produced them


@dataclass
class ArtifactNode:
    name: str
    kind: str
    rule_id: str
    task_id: str
    streaming: bool
    min_durability: str
    consumers: list[tuple[str, str]] = field(default_factory=list)  # (rule_id, task_id)
    durability: str = "local"

    def as_lock(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "producer": self.rule_id,
            "task": self.task_id,
            "durability": self.durability,
            "consumers": sorted(r for r, _ in self.consumers),
        }


@dataclass
class ArtifactGraph:
    artifacts: dict[str, ArtifactNode] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)


def _contract(rule: Any) -> Any | None:
    impl = rule.implementation
    if impl.kind != "builtin":
        return None
    c = op_catalog().get(impl.op)
    return c if c is not None and c.status == "available" else None


def derive_artifacts(spec: PipelineSpec) -> ArtifactGraph:
    g = ArtifactGraph()
    ordered = spec.ordered_rules()
    all_outputs = {o for _, r in ordered for o in r.outputs}
    overrides = spec.pipeline.artifacts
    for task, rule in ordered:
        loc = f"rules[{rule.rule_id}]"
        impl = rule.implementation
        contract = _contract(rule)
        if impl.kind == "builtin" and contract is None:
            continue  # planned op: reported as op_not_available
        # ---- inputs
        free_ports = list(contract.inputs) if contract is not None else []
        for name in rule.inputs:
            node = g.artifacts.get(name)
            if node is None:
                code, why = (
                    ("dataflow_order", "is produced by a later rule")
                    if name in all_outputs
                    else ("dataflow_gap", "is not produced by any rule")
                )
                g.issues.append(issue(code, RULES, f"{loc}.inputs", f"input '{name}' {why}"))
                continue
            if node.task_id != task.task_id and node.task_id not in spec.upstream(task.task_id):
                g.issues.append(
                    issue(
                        "dataflow_order",
                        RULES,
                        f"{loc}.inputs",
                        f"input '{name}' comes from task '{node.task_id}', which is not upstream of '{task.task_id}'",
                    )
                )
            if node.task_id != task.task_id and node.kind in NO_CROSS_KINDS:
                g.issues.append(
                    issue(
                        "dataflow_not_staged",
                        RULES,
                        f"{loc}.inputs",
                        f"'{name}' is a stream of files and cannot cross from task '{node.task_id}'",
                    )
                )
            node.consumers.append((rule.rule_id, task.task_id))
            if contract is not None:
                port = next((p for p in free_ports if p.kind == node.kind), None)
                if port is None:
                    kinds = sorted({p.kind for p in contract.inputs}) or ["none"]
                    g.issues.append(
                        issue(
                            "artifact_kind_mismatch",
                            RULES,
                            f"{loc}.inputs",
                            f"'{name}' is a {node.kind}; {impl.op} takes {', '.join(kinds)}",
                        )
                    )
                else:
                    free_ports.remove(port)
        for p in free_ports:
            if p.required:
                g.issues.append(
                    issue("dataflow_gap", RULES, f"{loc}.inputs", f"{impl.op} needs an input of kind '{p.kind}'")
                )
        # ---- outputs
        if contract is not None:
            ports = [(p.kind, p.streaming, p.min_durability) for p in contract.outputs]
        elif impl.kind == "expression":
            ports = [("value", False, "local")] if rule.category == "naming" else []
        else:  # custom
            ports = [(impl.output_kinds.get(o, ""), False, "local") for o in rule.outputs]
            missing = [o for o in rule.outputs if o not in impl.output_kinds]
            if missing:
                g.issues.append(
                    issue(
                        "output_arity_mismatch",
                        RULES,
                        f"{loc}.implementation.output_kinds",
                        f"declare the kind of outputs {missing}",
                    )
                )
        if len(rule.outputs) != len(ports):
            g.issues.append(
                issue(
                    "output_arity_mismatch",
                    RULES,
                    f"{loc}.outputs",
                    f"lists {len(rule.outputs)} outputs; this implementation produces {len(ports)}",
                )
            )
        for name, (kind, streaming, min_d) in zip(rule.outputs, ports, strict=False):
            if name in g.artifacts:
                g.issues.append(
                    issue(
                        "dataflow_multiple_producers",
                        RULES,
                        f"{loc}.outputs",
                        f"'{name}' is already produced by rule '{g.artifacts[name].rule_id}'",
                    )
                )
                continue
            g.artifacts[name] = ArtifactNode(name, kind or "value", rule.rule_id, task.task_id, streaming, min_d)
    # ---- durability and unused outputs
    for name, node in g.artifacts.items():
        crosses = any(t != node.task_id for _, t in node.consumers)
        if node.kind in TERMINAL_KINDS and node.kind == "receipt":
            node.durability = "final"
        elif (
            crosses or node.min_durability == "staged" or (name in overrides and overrides[name].durability == "staged")
        ):
            node.durability = "staged"
        elif node.streaming and node.consumers:
            node.durability = "stream"
        else:
            node.durability = "local"
        if not node.consumers and node.kind not in TERMINAL_KINDS:
            g.issues.append(
                issue("artifact_unused", RULES, f"rules[{node.rule_id}].outputs", f"artifact '{name}' is never used")
            )
    return g
