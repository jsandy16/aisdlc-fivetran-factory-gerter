"""spec.lock.json: the canonical, normalised spec plus hashes.

logic_sha     pipeline + rules + integrations + security, without cosmetic metadata (names, owners, labels, versions):
              identical in dev, test, ppt and prod. This is the evidence that what was tested is what runs.
binding_sha   per environment: that environment's bindings and destination allow-list.
derived_sha   the artifact graph and the contracts of the ops used.
Platform settings and engine decisions are not part of the lock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from factory_core.canonical import doc_sha256, pretty_json
from factory_core.envelope import CommandError, Issue

from .catalog import op_catalog
from .codes import issue
from .derive import derive_artifacts
from .load import SUPPORTED_SCHEMA
from .spec import PipelineSpec

LOCK_FILE = "spec.lock.json"
LOCK_VERSION = 1
COSMETIC_METADATA = ("name", "description", "owners", "tags", "requirements", "spec_version")


def _by_id(items: list[dict[str, Any]], key: str = "id") -> list[dict[str, Any]]:
    return sorted(items, key=lambda x: str(x.get(key, "")))


def _strip_labels(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_labels(v) for k, v in node.items() if k != "label"}
    if isinstance(node, list):
        return [_strip_labels(v) for v in node]
    return node


def normalised_logic(spec: PipelineSpec) -> dict[str, Any]:
    pipeline = spec.pipeline.model_dump(mode="json")
    pipeline["orchestration"]["tasks"] = _by_id(pipeline["orchestration"]["tasks"], "task_id")
    rules = {"rules": _by_id(spec.rules.model_dump(mode="json")["rules"], "rule_id")}
    integ = spec.integrations.model_dump(mode="json")
    for k in ("sources", "apis", "destinations"):
        integ[k] = _by_id(integ[k])
    if integ.get("portal"):
        integ["portal"]["endpoints"] = _by_id(integ["portal"]["endpoints"])
    for api in integ["apis"]:
        api["endpoints"] = _by_id(api["endpoints"])
    return {
        "pipeline": pipeline,
        "rules": rules,
        "integrations": integ,
        "security": spec.security.model_dump(mode="json"),
    }


def hashed_logic(logic: dict[str, Any]) -> dict[str, Any]:
    doc = json.loads(json.dumps(logic))
    md = doc["pipeline"]["metadata"]
    for k in COSMETIC_METADATA:
        md.pop(k, None)
    return _strip_labels(doc)  # type: ignore[no-any-return]


def normalised_bindings(spec: PipelineSpec) -> dict[str, Any]:
    envs = spec.environments.model_dump(mode="json")["environments"]
    dests = spec.destinations.model_dump(mode="json")["destinations"]
    return {
        env: {"environment": envs.get(env), "destinations": _by_id(dests.get(env, []))}
        for env in sorted(set(envs) | set(dests))
    }


def build_lock(spec: PipelineSpec) -> dict[str, Any]:
    graph = derive_artifacts(spec)
    logic = normalised_logic(spec)
    bindings = normalised_bindings(spec)
    ops_used = sorted({r.implementation.op for r in spec.rules.rules if r.implementation.kind == "builtin"})
    derived = {
        "artifacts": {n: graph.artifacts[n].as_lock() for n in sorted(graph.artifacts)},
        "op_contracts": {
            op: {"version": op_catalog()[op].version, "contract_sha": op_catalog()[op].contract_sha}
            for op in ops_used
            if op in op_catalog()
        },
    }
    pid = spec.pipeline_id
    return {
        "lock_version": LOCK_VERSION,
        "api_version": "factory/v1",
        "schema_version": spec.pipeline.schema_version,
        "pipeline_id": pid,
        "spec_version": spec.pipeline.metadata.spec_version,
        "logic": logic,
        "bindings": bindings,
        "derived": derived,
        "hashes": {
            "logic_sha": doc_sha256("factory.logic", hashed_logic(logic), pipeline_id=pid),
            "binding_sha": {
                env: doc_sha256("factory.binding", b, env=env, pipeline_id=pid) for env, b in bindings.items()
            },
            "derived_sha": doc_sha256("factory.derived", derived, pipeline_id=pid),
        },
    }


def lock_text(spec: PipelineSpec) -> str:
    return pretty_json(build_lock(spec))


def write_lock(spec: PipelineSpec) -> Path:
    path = spec.path / LOCK_FILE
    path.write_text(lock_text(spec), encoding="utf-8", newline="\n")
    return path


def check_lock(spec: PipelineSpec) -> list[Issue]:
    path = spec.path / LOCK_FILE
    if not path.is_file():
        return [issue("lock_missing", LOCK_FILE, "", f"{LOCK_FILE} does not exist")]
    if path.read_text(encoding="utf-8") != lock_text(spec):
        return [issue("lock_stale", LOCK_FILE, "", f"{LOCK_FILE} does not match the spec")]
    return []


def require_valid(errors: list[Issue]) -> None:
    if errors:
        raise CommandError("fix the validation errors before locking", "invalid", errors)


__all__ = ["LOCK_FILE", "SUPPORTED_SCHEMA", "build_lock", "check_lock", "lock_text", "write_lock"]
