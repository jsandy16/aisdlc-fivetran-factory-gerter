"""ChangePlan (ADR-0018): why (semantic diff), what files (two in-memory builds), what infrastructure (resource diff).

The base is a git ref (the pipeline folder at that commit) or a folder holding the previous pipeline. Both sides are
rendered exactly as `factory build` would, with the same platform settings and the same price basis, so the plan shows
only what the spec change causes. Offline, the infrastructure delta is a diff of resource addresses in the generated
`.tf.json`; `terraform plan -json` against real state is Phase 8.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from factory_core.canonical import canonical_bytes
from factory_core.envelope import CommandError
from factory_spec.acceptance import acceptance_digest, acceptance_problem
from factory_spec.load import load_spec
from factory_spec.repo import MARKER, Repo, load_repo
from factory_spec.spec import PipelineSpec

from ..gen.build import Rendered, render
from ..gen.cost import cost_report
from ..gen.names import dag_id
from .diff import spec_diff

STATEFUL = ("aws_sqs_queue", "aws_s3_bucket", "aws_dynamodb_table", "aws_kms_key", "aws_cloudwatch_log_group")
CHANGES_DIR = "changes"


def _git(cwd: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)  # noqa: S603, S607
    if proc.returncode:
        raise CommandError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()[:200]}", "config")
    return proc.stdout


def materialise_base(repo: Repo, pipeline_dir: Path, base: str, into: Path) -> Path:
    """Copy the base pipeline into a scratch repo that shares this repo's settings; return its pipeline folder."""
    if repo.root is None:
        raise CommandError(f"no {MARKER} found: run inside a team repository", "config")
    root = into / "repo"
    dest = root / "pipelines" / pipeline_dir.name
    cfg = repo.config.model_dump(mode="json")
    if cfg.get("platform_settings"):
        cfg["platform_settings"] = str((repo.root / cfg["platform_settings"]).resolve())
    root.mkdir(parents=True)
    lines = [f"{k} = {json.dumps(v)}" for k, v in sorted(cfg.items()) if v is not None]
    (root / MARKER).write_text("\n".join(lines) + "\n", encoding="utf-8")
    folder = Path(base)
    if folder.is_dir():
        shutil.copytree(folder, dest)
        return dest
    top = Path(_git(pipeline_dir, "rev-parse", "--show-toplevel").decode().strip())
    rel = pipeline_dir.resolve().relative_to(top.resolve()).as_posix()
    names = [n for n in _git(top, "ls-tree", "-r", "-z", "--name-only", base, "--", rel).decode().split("\0") if n]
    if not names:
        raise CommandError(f"'{rel}' does not exist at {base}", "not_found")
    for name in names:
        out = dest / Path(name).relative_to(rel)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_git(top, "show", f"{base}:{name}"))
    return dest


def _spec(d: Path) -> PipelineSpec:
    spec = load_spec(d).spec
    if spec is None:
        raise CommandError(f"the spec at {d.name} does not load", "invalid")
    return spec


def _major(version: str) -> int:
    return int(version.split(".", 1)[0])


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("-", 1)[0].split("+", 1)[0].split("."))


def _files(a: Rendered, b: Rendered) -> dict[str, list[str]]:
    fa, fb = a.manifest["files"], b.manifest["files"]
    return {"added": sorted(set(fb) - set(fa)), "removed": sorted(set(fa) - set(fb)),
            "changed": sorted(k for k in set(fa) & set(fb) if fa[k] != fb[k])}  # fmt: skip


def _resources(r: Rendered, env: str) -> dict[str, Any]:
    raw = r.files.get(f"terraform/{env}/main.tf.json")
    doc = json.loads(raw) if raw else {}
    return {f"{t}.{n}": body for t, items in doc.get("resource", {}).items() for n, body in items.items()}


def _tf(a: Rendered, b: Rendered) -> dict[str, dict[str, list[str]]]:
    out = {}
    for env in sorted(set(a.manifest["environments"]) | set(b.manifest["environments"])):
        ra, rb = _resources(a, env), _resources(b, env)
        delta = {"create": sorted(set(rb) - set(ra)), "update": sorted(k for k in set(ra) & set(rb) if ra[k] != rb[k]),
                 "destroy": sorted(set(ra) - set(rb))}  # fmt: skip
        if any(delta.values()):
            out[env] = delta
    return out


def _custom_review(base: PipelineSpec, head: PipelineSpec, bdir: Path, hdir: Path) -> list[dict[str, str]]:
    before = {r.rule_id: r for r in base.rules.rules if r.implementation.kind == "custom"}
    out = []
    for r in head.rules.rules:
        if r.implementation.kind != "custom":
            continue
        problem = acceptance_problem(r, hdir / "rules" / r.rule_id)
        old = before.get(r.rule_id)
        changed = old is None or (
            acceptance_digest(old, bdir / "rules" / r.rule_id)["digest"]
            != acceptance_digest(r, hdir / "rules" / r.rule_id)["digest"]
        )
        if problem or changed:
            why = problem or ("new custom rule" if old is None else "contract, plain English, code or tests changed")
            out.append({"rule_id": r.rule_id, "why": why, "accepted": "no" if problem else "yes"})
    return out


def _money(a: dict[str, Any], b: dict[str, Any], key: str, q: str) -> dict[str, str]:
    da, db = Decimal(a[key]), Decimal(b[key])
    return {"before": a[key], "after": b[key], "delta": str((db - da).quantize(Decimal(q)))}


def change_plan(repo: Repo, pipeline_dir: Path, base: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="factory-base-") as tmp:
        bdir = materialise_base(repo, pipeline_dir, base, Path(tmp))
        bspec, hspec = _spec(bdir), _spec(pipeline_dir)
        brepo = load_repo(bdir)
        brepo.calibrated = repo.calibrated  # both sides on the same price basis
        before, after = render(brepo, bdir), render(repo, pipeline_dir)
        review = _custom_review(bspec, hspec, bdir, pipeline_dir)
    changes = spec_diff(bspec, hspec)
    bv, hv = bspec.pipeline.metadata.spec_version, hspec.pipeline.metadata.spec_version
    issues: list[dict[str, str]] = []
    if bspec.pipeline_id != hspec.pipeline_id:
        issues.append({"code": "pipeline_id_changed", "msg": "pipeline_id never changes; create a new pipeline"})
    breaking = [c for c in changes if c.breaking]
    if breaking and _major(hv) <= _major(bv):
        issues.append({"code": "breaking_change_needs_major",
                       "msg": f"{len(breaking)} breaking change(s) ({breaking[0].path}); bump spec_version to "
                              f"{_major(bv) + 1}.0.0 for a new DAG"})  # fmt: skip
    material = [c for c in changes if c.area not in ("version", "metadata")]
    if material and _version(hv) <= _version(bv):
        issues.append({"code": "spec_version_not_bumped", "msg": f"the spec changed; raise spec_version above {bv}"})
    files = _files(before, after)
    tf = _tf(before, after)
    bdag, hdag = dag_id(bspec.pipeline_id, bv), dag_id(hspec.pipeline_id, hv)
    touched_rules = sorted({c.path.split("[", 1)[1].split("]", 1)[0] for c in changes if c.path.startswith("rules.")})
    head_rules = {r.rule_id: r for r in hspec.rules.rules}
    tasks = sorted({t.task_id for t in hspec.pipeline.orchestration.tasks if set(t.rules) & set(touched_rules)})
    approvals: dict[tuple[str, str], None] = {}
    for c in changes:
        if c.approver:
            approvals[(c.approver, f"{c.area}: {c.path}")] = None
    for env, d in tf.items():
        for addr in d["destroy"]:
            if addr.split(".", 1)[0] in STATEFUL:
                approvals[("platform", f"destroys stateful {addr} in {env}")] = None
    bc, hc = cost_report(bspec, repo.prices), cost_report(hspec, repo.prices)
    plan = {
        "pipeline_id": hspec.pipeline_id,
        "spec_version": {"before": bv, "after": hv},
        "spec_changes": [c.as_dict() for c in changes],
        "breaking": bool(breaking),
        "dag_strategy": {
            "before": bdag,
            "after": hdag,
            "strategy": "new_dag" if bdag != hdag else "keep",
            **(
                {"retire": bdag, "how": "pause the old DAG, let its runs drain, then remove it"} if bdag != hdag else {}
            ),
        },
        "files": files,
        "terraform": tf,
        "rules_to_review": review,
        "tests_to_run": {
            "rules": [r for r in touched_rules if r in head_rules and head_rules[r].test_cases],
            "tasks": tasks,
            "dag_integrity": any(f.startswith("dags/") for v in files.values() for f in v),
        },
        "docs_to_regen": sorted({f for v in files.values() for f in v if f.startswith("docs/")}),
        "cost": {
            "basis": hc["basis"],
            "monthly_usd": _money(bc, hc, "monthly_usd", "0.01"),
            "per_run_usd": _money(bc, hc, "per_run_usd", "0.0001"),
            "within_budget": hc["within_budget"],
        },
        "requires_approval": [{"who": w, "why": y} for w, y in approvals],
        "bundle_sha256": {"before": before.manifest["bundle_sha256"], "after": after.manifest["bundle_sha256"]},
    }
    plan["plan_sha256"] = hashlib.sha256(canonical_bytes(plan)).hexdigest()
    return {"ok": not issues, "issues": issues, "plan": plan}


def record_change(pipeline_dir: Path, request: str, plan: dict[str, Any]) -> Path:
    """changes/<n>.json: the request, the plan and its hash; approvers are filled from the PR, never by the CLI."""
    d = pipeline_dir / CHANGES_DIR
    d.mkdir(exist_ok=True)
    n = 1 + max((int(p.stem) for p in d.glob("[0-9][0-9][0-9][0-9].json")), default=0)
    entry = {"n": n, "request": request, "spec_version": plan["spec_version"], "plan_sha256": plan["plan_sha256"],
             "plan": plan, "approvers": []}  # fmt: skip
    out = d / f"{n:04d}.json"
    out.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
