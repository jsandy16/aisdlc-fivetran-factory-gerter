"""Semantic spec diff by stable ids (task_id, rule_id, id), classified as breaking or not (ADR-0018).

Breaking = DAG shape or run identity changes: a task added or removed, `depends_on`, the trigger type, the run key.
It needs a major `spec_version` bump and a new `<pipeline>__v<major>` DAG. Everything else keeps the DAG; run pinning
protects in-flight runs. Security, governance-owned bindings and destinations are flagged for governance approval.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from factory_spec.lock import normalised_bindings, normalised_logic
from factory_spec.spec import PipelineSpec

ID_KEYS = ("task_id", "rule_id", "id")
MISSING = object()
_TASK = r"^pipeline\.orchestration\.tasks\[[^\]]+\]"
CLASSES: list[tuple[re.Pattern[str], str, bool, str | None]] = [  # first match wins: (path, area, breaking, approver)
    (re.compile(r"^pipeline\.metadata\.spec_version$"), "version", False, None),
    (re.compile(r"^pipeline\.metadata\."), "metadata", False, None),
    (re.compile(_TASK + r"$"), "dag_shape", True, "platform"),
    (re.compile(_TASK + r"\.depends_on$"), "dag_shape", True, "platform"),
    (re.compile(r"^pipeline\.trigger\.type$"), "run_identity", True, "platform"),
    (re.compile(r"^pipeline\.reliability\.run_key$"), "run_identity", True, "platform"),
    (re.compile(r"^pipeline\."), "pipeline", False, None),
    (re.compile(r"^rules\."), "rules", False, None),
    (re.compile(r"^integrations\.destinations"), "destinations", False, "governance"),
    (re.compile(r"^integrations\."), "integrations", False, None),
    (re.compile(r"^security\."), "security", False, "governance"),
    (re.compile(r"^bindings\.[^.]+\.destinations"), "destinations", False, "governance"),
    (re.compile(r"^bindings\."), "bindings", False, None),
]


@dataclass(frozen=True)
class Change:
    path: str
    kind: str  # added | removed | changed
    area: str
    breaking: bool
    approver: str | None
    before: Any = None
    after: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _id_of(item: Any) -> str | None:
    if isinstance(item, dict):
        for k in ID_KEYS:
            if isinstance(item.get(k), str):
                return str(item[k])
    return None


def _keyed(node: Any) -> Any:
    """Lists of id-bearing objects become maps by id, so a reorder is no change and a rename is remove + add."""
    if isinstance(node, dict):
        return {k: _keyed(v) for k, v in node.items()}
    if isinstance(node, list) and node and all(_id_of(x) for x in node):
        return {f"[{_id_of(x)}]": _keyed(x) for x in node}
    return node


def _walk(a: Any, b: Any, path: str, out: list[tuple[str, str, Any, Any]]) -> None:
    if a == b:
        return
    if isinstance(a, dict) and isinstance(b, dict) and not (_listish(a) ^ _listish(b)):
        for k in sorted(set(a) | set(b)):
            sub = f"{path}{k}" if k.startswith("[") else f"{path}.{k}" if path else k
            if k not in a:
                out.append((sub, "added", None, b[k]))
            elif k not in b:
                out.append((sub, "removed", a[k], None))
            else:
                _walk(a[k], b[k], sub, out)
        return
    out.append((path, "changed", a, b))


def _listish(d: dict[str, Any]) -> bool:
    return bool(d) and all(k.startswith("[") for k in d)


def _doc(spec: PipelineSpec) -> dict[str, Any]:
    return _keyed({**normalised_logic(spec), "bindings": normalised_bindings(spec)})  # type: ignore[no-any-return]


def _short(v: Any) -> Any:
    return v if isinstance(v, str | int | float | bool | list) or v is None else "{…}"


def spec_diff(base: PipelineSpec, head: PipelineSpec) -> list[Change]:
    raw: list[tuple[str, str, Any, Any]] = []
    _walk(_doc(base), _doc(head), "", raw)
    changes = []
    for path, kind, before, after in raw:
        area, breaking, approver = "other", False, None
        for rx, ar, br, ap in CLASSES:
            if rx.search(path):
                area, breaking, approver = ar, br, ap
                break
        changes.append(Change(path, kind, area, breaking, approver, _short(before), _short(after)))
    return changes
