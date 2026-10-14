"""`factory build`: validate → bundle → plan → per environment DAG + Terraform + egress → cost → docs, then a
deterministic BUILD-MANIFEST.json (sha256 of every output). No timestamps: the same inputs give the same bytes."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from factory_core.canonical import canonical_bytes
from factory_core.envelope import CommandError
from factory_spec.ba_view import render_ba
from factory_spec.bundle import build_bundle
from factory_spec.lock import build_lock
from factory_spec.repo import Repo

from ..plan.engine import plan
from .cost import cost_report
from .dag import generate_dag
from .names import deploy_for
from .terraform import egress, generate_terraform


def _json(doc: Any) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()


def build(repo: Repo, pipeline_dir: Path, out_root: Path, *, image: str | None = None) -> dict[str, Any]:
    bundle = build_bundle(pipeline_dir)  # validates; refuses a stale lock
    from factory_spec.load import load_spec

    spec = load_spec(pipeline_dir).spec
    if spec is None:  # pragma: no cover - build_bundle validated it
        raise CommandError("the spec does not load", "invalid")
    pid, team = spec.pipeline_id, spec.pipeline.metadata.team
    files: dict[str, bytes] = {
        f"bundle/{bundle.sha256}.zip": bundle.data,
        "plan.json": _json(plan(spec)),
        "cost.json": _json(cost_report(spec)),
        "docs/spec.md": render_ba(spec).encode(),
    }
    logic = build_lock(spec)["hashes"]["logic_sha"]
    envs = [e for e, b in sorted(spec.environments.environments.items()) if b.enabled and e in repo.platform]
    for env in envs:
        deploy = deploy_for(repo.platform[env], team, pid, bundle.sha256, image)
        files[f"dags/{env}/{pid}.py"] = generate_dag(spec, deploy, logic).encode()
        for name, doc in generate_terraform(spec, repo.platform[env], deploy).items():
            files[f"terraform/{env}/{name}"] = _json(doc)
        files[f"terraform/{env}/egress.json"] = _json(egress(spec, env))
    manifest = {
        "pipeline_id": pid,
        "logic_sha": logic,
        "bundle_sha256": bundle.sha256,
        "environments": envs,
        "files": {k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())},
    }
    out = out_root / pid
    if out.exists():
        shutil.rmtree(out)
    for rel, data in files.items():
        f = out / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    (out / "BUILD-MANIFEST.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return {"out": str(out), "bundle_sha256": bundle.sha256, "environments": envs, "files": len(files),
            "manifest_sha256": hashlib.sha256(canonical_bytes(manifest)).hexdigest()}  # fmt: skip
