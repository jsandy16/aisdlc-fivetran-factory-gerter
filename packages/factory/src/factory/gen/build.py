"""`factory build`: validate → bundle → plan → per environment DAG + Terraform + egress → cost → docs, then a
deterministic BUILD-MANIFEST.json (sha256 of every output). No timestamps: the same inputs give the same bytes."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory_core.canonical import canonical_bytes
from factory_core.envelope import CommandError
from factory_spec.ba_view import render_ba
from factory_spec.bundle import build_bundle
from factory_spec.lock import build_lock
from factory_spec.repo import Repo

from ..plan.engine import plan
from . import names
from .cost import cost_report
from .dag import generate_dag
from .docs import pipeline_docs
from .names import deploy_for
from .ownership import contract_markdown
from .terraform import egress, generate_stack, runtime_env

MANIFEST = "BUILD-MANIFEST.json"


def _json(doc: Any) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()


@dataclass(frozen=True)
class Rendered:
    """Everything `factory build` writes, in memory: `verify` and the change engine compare these."""

    pipeline_id: str
    files: dict[str, bytes]
    manifest: dict[str, Any]

    @property
    def manifest_bytes(self) -> bytes:
        return canonical_bytes(self.manifest) + b"\n"


def render(repo: Repo, pipeline_dir: Path, *, image: str | None = None) -> Rendered:
    bundle = build_bundle(pipeline_dir)  # validates; refuses a stale lock
    from factory_spec.load import load_spec

    spec = load_spec(pipeline_dir).spec
    if spec is None:  # pragma: no cover - build_bundle validated it
        raise CommandError("the spec does not load", "invalid")
    prices = repo.prices
    pid, team = spec.pipeline_id, spec.pipeline.metadata.team
    dag_id = names.dag_id(pid, spec.pipeline.metadata.spec_version)
    files: dict[str, bytes] = {
        f"bundle/{bundle.sha256}.zip": bundle.data,
        "plan.json": _json(plan(spec, prices)),
        "cost.json": _json(cost_report(spec, prices)),
        "docs/spec.md": render_ba(spec).encode(),
    }
    logic = build_lock(spec)["hashes"]["logic_sha"]
    envs = [e for e, b in sorted(spec.environments.environments.items()) if b.enabled and e in repo.platform]
    deploys = {}
    for env in envs:
        deploy = deploys[env] = deploy_for(repo.platform[env], team, pid, bundle.sha256, image)
        pod_env = runtime_env(spec, repo.platform[env], deploy)
        infra = spec.environments.environments[env].infrastructure
        fn_names = dict(infra.compute.names) if infra.mode_of("compute") == "existing" else {}
        files[f"dags/{env}/{dag_id}.py"] = generate_dag(spec, deploy, logic, prices, pod_env, fn_names).encode()
        stack, contract = generate_stack(spec, repo.platform[env], deploy, prices)
        for name, doc in stack.items():
            files[f"terraform/{env}/{name}"] = _json(doc)
        if contract["provide"]:  # the handover to the infra team: exactly what this stack no longer creates
            files[f"infra/{env}.json"] = _json(contract)
            files[f"infra/{env}.md"] = contract_markdown(pid, env, contract).encode()
        files[f"terraform/{env}/egress.json"] = _json(egress(spec, env))
    files |= {f"docs/{k}": v.encode() for k, v in pipeline_docs(spec, deploys, prices).items()}
    manifest = {
        "pipeline_id": pid,
        "dag_id": dag_id,
        "spec_version": spec.pipeline.metadata.spec_version,
        "logic_sha": logic,
        "bundle_sha256": bundle.sha256,
        "pricing": {
            "version": prices.version,
            "basis": prices.status,
            "sha256": hashlib.sha256(canonical_bytes(prices.model_dump(mode="json"))).hexdigest(),
        },
        "environments": envs,
        "image": image,
        "files": {k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())},
    }
    return Rendered(pid, dict(sorted(files.items())), manifest)


def build(repo: Repo, pipeline_dir: Path, out_root: Path, *, image: str | None = None) -> dict[str, Any]:
    r = render(repo, pipeline_dir, image=image)
    out = out_root / r.pipeline_id
    if out.exists():
        shutil.rmtree(out)
    for rel, data in r.files.items():
        f = out / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    (out / MANIFEST).write_bytes(r.manifest_bytes)
    return {"out": str(out), "bundle_sha256": r.manifest["bundle_sha256"], "dag_id": r.manifest["dag_id"],
            "environments": r.manifest["environments"], "files": len(r.files),
            "manifest_sha256": hashlib.sha256(canonical_bytes(r.manifest)).hexdigest()}  # fmt: skip
