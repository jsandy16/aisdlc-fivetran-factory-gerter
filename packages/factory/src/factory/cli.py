"""`factory`: the Pipeline Factory command line.

Every command accepts `--json` (before the command name) and then prints exactly one Result envelope, including on
usage errors. Exit codes: 0 ok · 1 the command ran and found problems · 2 usage or environment error.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

import typer
from factory_core.envelope import CommandError, Issue, Result
from factory_spec.ba_view import render_ba
from factory_spec.files import SPEC_DIR
from factory_spec.lock import build_lock, check_lock, write_lock
from factory_spec.migrate import migrate_dir
from factory_spec.patch import SpecPatch, apply_patch, parse_value
from factory_spec.repo import MARKER, find_repo_root, load_repo
from factory_spec.schema_export import check_schemas, committed_dir, write_schemas
from factory_spec.validate import validate_dir
from typer.exceptions import Abort, Exit, TyperException

from factory_core import yamlio

app = typer.Typer(
    name="factory",
    help="Pipeline Factory: spec-driven ingestion pipelines (MWAA, EKS, Lambda, Fivetran).",
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
    no_args_is_help=True,
)
USAGE_CODES = frozenset({"usage", "no_repo", "not_found", "config"})


class _State:
    json: bool = False


STATE = _State()


@app.callback()
def _root(json_out: Annotated[bool, typer.Option("--json", help="Print one JSON Result envelope")] = False) -> None:
    STATE.json = json_out


def _emit(r: Result) -> int:
    if STATE.json:
        typer.echo(r.model_dump_json(indent=2))
    else:
        typer.echo(f"{r.command}: {'OK' if r.ok else 'PROBLEMS'}")
        for i in r.issues:
            where = f"{i.file}{(':' + i.loc) if i.loc else ''}"
            typer.echo(f"  {i.severity:7} {i.code:32} {where}: {i.msg}")
            if i.hint and i.severity == "error":
                typer.echo(f"  {'':7} {'':32} fix: {i.hint}")
        if r.data:
            for k, v in r.data.items():
                if isinstance(v, str) and "\n" in v:
                    typer.echo(v)
                else:
                    typer.echo(f"  {k}: {json.dumps(v, sort_keys=True, default=str)}")
    return 0 if r.ok else 1


def _pipeline_dir(name: str) -> Path:
    root = find_repo_root()
    if root is None:
        raise CommandError(f"not inside a pipelines repository (no {MARKER} found); run `factory init`", "no_repo")
    d = root / "pipelines" / name
    if not (d / SPEC_DIR).is_dir():
        raise CommandError(f"pipeline '{name}' not found at pipelines/{name}/{SPEC_DIR}", "not_found")
    return d


# ---------------------------------------------------------------- commands


@app.command("init")
def init(
    path: Annotated[Path, typer.Argument(help="Folder to turn into a pipelines repository")] = Path("."),
    team: Annotated[str | None, typer.Option(help="Team owning the repository")] = None,
    platform_settings: Annotated[str | None, typer.Option(help="Path to the platform settings folder")] = None,
    layout: Annotated[str, typer.Option(help="mwaa (Python DAGs, Lambda, pods) or dataops (DAG JSON, containers)")] = (
        "mwaa"
    ),
    environments: Annotated[
        str | None, typer.Option(help="Comma-separated (default dev,test,ppt,prod; dataops: dev,sit,ppt,prd)")
    ] = None,
    project: Annotated[str | None, typer.Option(help="dataops: project name, e.g. rd-dpl-studydata")] = None,
    dist_bucket: Annotated[str | None, typer.Option(help="dataops: bucket the containers sync src/ from")] = None,
    base_image: Annotated[str | None, typer.Option(help="dataops: FROM image of docker/Dockerfile")] = None,
    ca_bundle: Annotated[str | None, typer.Option(help="dataops: corporate CA bundle path in the image")] = None,
    proxy: Annotated[str | None, typer.Option(help="dataops: HTTP(S) proxy the image sets")] = None,
    no_proxy: Annotated[str | None, typer.Option(help="dataops: hosts that bypass the proxy")] = None,
) -> int:
    if layout not in ("mwaa", "dataops"):
        raise CommandError("--layout is mwaa or dataops", "usage")
    root = path.resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER
    envs = [e.strip() for e in environments.split(",")] if environments else (
        ["dev", "sit", "ppt", "prd"] if layout == "dataops" else ["dev", "test", "ppt", "prod"])  # fmt: skip
    data: dict[str, Any] = {"root": str(root), "layout": layout}
    issues: list[Issue] = []
    if not marker.exists():
        lines = ["# Pipeline Factory team repository", 'api_version = "factory/v1"']
        if team:
            lines.append(f'team = "{team}"')
        lines.append("environments = " + json.dumps(envs))
        settings = platform_settings or ("./platform-settings" if layout == "dataops" else None)
        if settings:
            lines.append(f'platform_settings = "{settings}"')
        if layout == "dataops":
            missing = [n for n, v in (("--project", project), ("--dist-bucket", dist_bucket),
                                      ("--base-image", base_image), ("--team", team)) if not v]  # fmt: skip
            if missing:
                raise CommandError(f"the dataops layout needs {', '.join(missing)}", "usage")
            lines += ['layout = "dataops"', "", "[dataops]", f'project = "{project}"', f'dist_bucket = "{dist_bucket}"',
                      f'base_image = "{base_image}"']  # fmt: skip
            lines += [f'{k} = "{v}"' for k, v in (("ca_bundle", ca_bundle), ("proxy", proxy), ("no_proxy", no_proxy))
                      if v]  # fmt: skip
        marker.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "pipelines").mkdir(exist_ok=True)
    gi = root / ".gitignore"
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    wanted = [".venv/", "__pycache__/", "*.pyc", ".factory/"]
    gi.write_text("\n".join(existing + [w for w in wanted if w not in existing]) + "\n", encoding="utf-8")
    repo = load_repo(root)
    if repo.config.layout == "dataops" and repo.config.dataops is not None:
        from .gen.dataops import skeleton

        written, kept = [], []
        for rel, text in skeleton.files(repo.config.dataops, repo.config.team or "team").items():
            f = root / rel
            if f.exists():
                kept.append(rel)
                continue
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
            written.append(rel)
        ps_dir = root / (repo.config.platform_settings or "platform-settings")
        for env in repo.environments:
            f = ps_dir / f"{env}.json"
            if not f.exists():
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(skeleton.platform_template(env, repo.config.team or "team"), indent=2) + "\n",
                             encoding="utf-8")  # fmt: skip
                written.append(f.relative_to(root).as_posix())
        data |= {"written": written, "kept": kept}
        if kept:
            data["add_to_existing_files"] = skeleton.additions(repo.config.dataops)
        repo = load_repo(root)  # the templates just written: they report what is still to fill in
        issues += [Issue(code="platform_settings_missing", severity="warning", msg=m) for m in repo.platform_issues]
    return _emit(Result(command="init", ok=True, data=data, issues=issues))


def _fit_environments(spec_dir: Path, envs: list[str], layout: str) -> None:
    """Give a starter one binding block per repository environment (a starter carries dev, test, ppt and prod);
    an environment the starter lacks gets a copy of its dev block. The dataops layout orchestrates on `dataops`."""
    import copy

    for fname, key in (("environments.yaml", "environments"), ("destinations.yaml", "destinations")):
        f = spec_dir / fname
        doc = yamlio.load_path(f)
        blocks = doc.get(key)
        if not blocks or (list(blocks) == envs and layout != "dataops"):
            continue
        template = blocks.get("dev") or next(iter(blocks.values()))
        fitted = {env: copy.deepcopy(blocks.get(env, template)) for env in envs}
        for env in list(blocks):
            del blocks[env]
        for env, block in fitted.items():
            if layout == "dataops" and key == "environments" and "placement" in block:
                block["placement"]["orchestration"] = "dataops"
            blocks[env] = block
        yamlio.dump_path(doc, f)


def starters() -> dict[str, Path]:
    base = Path(str(resources.files("factory").joinpath("starters")))
    return {p.name: p for p in sorted(base.iterdir()) if (p / SPEC_DIR).is_dir()}


@app.command("new")
def new(
    name: Annotated[str, typer.Argument(help="Pipeline id (lower_snake_case)")],
    starter: Annotated[str, typer.Option(help="Starter template")] = "s3_package_deliver",
    team: Annotated[str | None, typer.Option(help="Team (default: factory.toml team)")] = None,
    force: Annotated[bool, typer.Option(help="Overwrite an existing pipeline")] = False,
) -> int:
    import re

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
        raise CommandError("the pipeline id must be lower_snake_case (letters, digits, _)", "usage")
    root = find_repo_root()
    if root is None:
        raise CommandError(f"not inside a pipelines repository (no {MARKER}); run `factory init`", "no_repo")
    all_starters = starters()
    if starter not in all_starters:
        raise CommandError(f"unknown starter '{starter}' (one of {sorted(all_starters)})", "usage")
    target = root / "pipelines" / name
    if target.exists() and not force:
        raise CommandError(f"pipelines/{name} already exists (use --force to overwrite)", "usage")
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(all_starters[starter] / SPEC_DIR, target / SPEC_DIR)
    repo = load_repo(root)
    _fit_environments(target / SPEC_DIR, repo.environments, repo.config.layout)
    pipe = target / SPEC_DIR / "pipeline.yaml"
    doc = yamlio.load_path(pipe)
    doc["metadata"]["pipeline_id"] = name
    if team or repo.config.team:
        doc["metadata"]["team"] = team or repo.config.team
    yamlio.dump_path(doc, pipe)
    res = validate_dir(target, repo)
    todo = [i for i in res.issues if i.code == "required"]
    return _emit(
        Result(
            command="new",
            ok=True,
            data={"path": f"pipelines/{name}", "starter": starter, "to_fill": len(todo)},
            issues=[i for i in res.issues if i.code != "required"],
        )
    )


@app.command("validate")
def validate(
    name: Annotated[str, typer.Argument(help="Pipeline id")],
    allow_unfilled: Annotated[bool, typer.Option(help="Do not fail on <REQUIRED> placeholders")] = False,
) -> int:
    d = _pipeline_dir(name)
    res = validate_dir(d)
    errors = [i for i in res.issues if i.severity == "error" and not (allow_unfilled and i.code == "required")]
    data: dict[str, Any] = {"unfilled": len([i for i in res.issues if i.code == "required"])}
    if res.load.spec is not None and not res.errors:
        lock = build_lock(res.load.spec)
        data["logic_sha"] = lock["hashes"]["logic_sha"]
    return _emit(Result(command="validate", ok=not errors, issues=res.issues, data=data))


@app.command("lock")
def lock(
    name: Annotated[str, typer.Argument(help="Pipeline id")],
    check: Annotated[bool, typer.Option(help="Fail if spec.lock.json is missing or stale (CI)")] = False,
) -> int:
    d = _pipeline_dir(name)
    res = validate_dir(d)
    if res.load.spec is None or res.errors:
        return _emit(
            Result(command="lock", ok=False, issues=res.errors, data={"error": "fix the validation errors first"})
        )
    if check:
        issues = check_lock(res.load.spec)
        return _emit(Result(command="lock --check", ok=not issues, issues=issues))
    path = write_lock(res.load.spec)
    hashes = build_lock(res.load.spec)["hashes"]
    return _emit(
        Result(command="lock", ok=True, data={"lock": str(path.relative_to(d.parent.parent)), "hashes": hashes})
    )


@app.command("show")
def show(
    name: Annotated[str, typer.Argument(help="Pipeline id")],
    view: Annotated[str, typer.Option(help="ba (plain English) or lock (canonical JSON)")] = "ba",
) -> int:
    if view not in ("ba", "lock"):
        raise CommandError("--view must be 'ba' or 'lock'", "usage")
    d = _pipeline_dir(name)
    res = validate_dir(d)
    if res.load.spec is None:
        return _emit(Result(command="show", ok=False, issues=res.issues))
    if view == "ba":
        text = render_ba(res.load.spec)
        if STATE.json:
            return _emit(Result(command="show", ok=True, data={"view": "ba", "text": text}))
        typer.echo(text, nl=False)
        return 0
    lock_doc = build_lock(res.load.spec)
    if STATE.json:
        return _emit(Result(command="show", ok=True, data={"view": "lock", "lock": lock_doc}))
    typer.echo(json.dumps(lock_doc, indent=2, sort_keys=True))
    return 0


@app.command("schema")
def schema(
    write: Annotated[Path | None, typer.Option(help="Folder to write *.schema.json into")] = None,
    check: Annotated[bool, typer.Option(help="Fail if the committed schemas differ (framework CI)")] = False,
) -> int:
    if check:
        diffs = check_schemas(committed_dir())
        issues = [
            Issue(
                code="schema_drift",
                file=d,
                msg="differs from the generated schema",
                hint="run `factory schema --write <packages/factory_spec/src/factory_spec/schemas>`",
            )
            for d in diffs
        ]
        return _emit(Result(command="schema --check", ok=not diffs, issues=issues))
    target = write or (Path.cwd() / ".factory" / "schemas")
    files = write_schemas(target)
    return _emit(Result(command="schema", ok=True, data={"written": files, "folder": str(target)}))


@app.command("migrate")
def migrate(
    name: Annotated[str, typer.Argument(help="Pipeline id")],
    to: Annotated[str | None, typer.Option(help="Target schema version (default: latest)")] = None,
    dry_run: Annotated[bool, typer.Option(help="Show what would change")] = False,
) -> int:
    d = _pipeline_dir(name)
    rep = migrate_dir(d, to=to, dry_run=dry_run)
    return _emit(
        Result(
            command="migrate",
            ok=True,
            data={
                "from": rep.from_version,
                "to": rep.to_version,
                "applied": rep.applied,
                "changed": rep.changed_files,
                "dry_run": dry_run,
            },
        )
    )


def _edit(op: str, name: str, file: str, path: str, value: str | None, keep_invalid: bool) -> int:
    d = _pipeline_dir(name)
    val = parse_value(value) if value is not None else None
    out = apply_patch(d, SpecPatch(file=file, op=op, path=path, value=val), keep_invalid=keep_invalid)  # type: ignore[arg-type]
    errors = [i for i in out.issues if i.severity == "error"]
    return _emit(Result(command=op, ok=not errors, issues=out.issues))


FileArg = Annotated[str, typer.Argument(help="Spec file, e.g. rules.yaml")]
PathArg = Annotated[str, typer.Argument(help="Selector path, e.g. rules[r_zip].implementation.params.level")]
KeepInvalid = Annotated[bool, typer.Option(help="Keep the edit even if it introduces errors")]


@app.command("set")
def set_(
    name: Annotated[str, typer.Argument()],
    file: FileArg,
    path: PathArg,
    value: Annotated[str, typer.Argument(help="YAML value")],
    keep_invalid: KeepInvalid = False,
) -> int:
    """Set a value (comments are kept; an edit that adds errors is rolled back)."""
    return _edit("set", name, file, path, value, keep_invalid)


@app.command("add")
def add(
    name: Annotated[str, typer.Argument()],
    file: FileArg,
    path: PathArg,
    value: Annotated[str, typer.Argument(help="YAML value")],
    keep_invalid: KeepInvalid = False,
) -> int:
    """Append a value to a list."""
    return _edit("add", name, file, path, value, keep_invalid)


@app.command("remove")
def remove(
    name: Annotated[str, typer.Argument()], file: FileArg, path: PathArg, keep_invalid: KeepInvalid = False
) -> int:
    """Remove a key or a list item."""
    return _edit("remove", name, file, path, None, keep_invalid)


@app.command("plan")
def plan_cmd(name: Annotated[str, typer.Argument(help="Pipeline id")]) -> int:
    """Print the engine decision of every task (catalog or calibrated prices; never written to the lock)."""
    from .plan.engine import plan

    d = _pipeline_dir(name)
    res = validate_dir(d)
    if res.load.spec is None or res.errors:
        return _emit(
            Result(command="plan", ok=False, issues=res.errors, data={"error": "fix the validation errors first"})
        )
    out = plan(res.load.spec, load_repo(d.parent.parent).prices)
    bad = [x for x in out["engine_decisions"] if x["choice"] is None]
    issues = [Issue(code="engine_infeasible", msg=f"{x['task_id']}: {x['reason']}", loc=x["task_id"]) for x in bad]
    if STATE.json:
        return _emit(Result(command="plan", ok=not bad, issues=issues, data=out))
    typer.echo(f"plan: {'OK' if not bad else 'PROBLEMS'}  (pricing {out['pricing']})")
    for x in out["engine_decisions"]:
        why = f"; filtered {[f['engine'] for f in x['filters']]}" if x["filters"] else ""
        typer.echo(f"  {x['task_id']:24} {x['requested']:>15} -> {x['choice'] or '-':14} {x['reason']}{why}")
    return 0 if not bad else 1


@app.command("cost")
def cost_cmd(name: Annotated[str, typer.Argument(help="Pipeline id")]) -> int:
    """Monthly and per-run cost from the planner against the spec's budget; says which price basis it used."""
    from .gen.cost import cost_report

    d = _pipeline_dir(name)
    res = validate_dir(d)
    if res.load.spec is None or res.errors:
        return _emit(Result(command="cost", ok=False, issues=res.errors))
    rep = cost_report(res.load.spec, load_repo(d.parent.parent).prices)
    issues = [] if rep["within_budget"] else [Issue(code="over_budget", msg="the estimate exceeds the budget")]
    return _emit(Result(command="cost", ok=rep["within_budget"], issues=issues, data=rep))


@app.command("bundle")
def bundle_cmd(name: Annotated[str, typer.Argument(help="Pipeline id")]) -> int:
    """Write the deterministic bundle to build/<pipeline>/bundle/ and print its sha256."""
    from factory_spec.bundle import build_bundle

    d = _pipeline_dir(name)
    b = build_bundle(d)
    out = d.parent.parent / "build" / name / "bundle" / f"{b.sha256}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b.data)
    return _emit(Result(command="bundle", ok=True, data={"sha256": b.sha256, "path": str(out), "bytes": len(b.data)}))


@app.command("test")
def test_cmd(name: Annotated[str, typer.Argument(help="Pipeline id")]) -> int:
    """Run the owner's test cases: pure rules here, custom rules through the plugin API (if the runtime is present)."""
    import tempfile

    from factory_spec.owner_tests import run_owner_tests

    d = _pipeline_dir(name)
    res = validate_dir(d)
    spec = res.load.spec
    if spec is None:
        return _emit(Result(command="test", ok=False, issues=res.issues))
    cases = [
        c.__dict__
        for c in run_owner_tests(spec)
        if c.status != "skipped" or c.detail != "custom rules are tested by their test_impl.py"
    ]
    run_case: Any = None
    with contextlib.suppress(ImportError):
        from factory_runtime.plugins import run_case
    for r in spec.rules.rules:
        if r.implementation.kind != "custom":
            continue
        for c in r.test_cases:
            if run_case is None:
                cases.append(
                    {"rule_id": r.rule_id, "case": c.name, "status": "skipped", "detail": "runtime not installed"}
                )
                continue
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    ok, why = run_case(d, r, c.given, c.expect, Path(tmp))
                except Exception as exc:  # a failing plugin is a failed case, reported with its type only
                    ok, why = False, f"raised {type(exc).__name__}"
            cases.append({"rule_id": r.rule_id, "case": c.name, "status": "pass" if ok else "fail", "detail": why})
    failed = [c for c in cases if c["status"] == "fail"]
    issues = [Issue(code="test_failed", msg=f"{c['rule_id']}/{c['case']}: {c['detail']}") for c in failed]
    summary = {s: sum(1 for c in cases if c["status"] == s) for s in ("pass", "fail", "skipped")}
    return _emit(Result(command="test", ok=not failed, issues=issues, data={**summary, "cases": cases}))


@app.command("build")
def build_cmd(
    name: Annotated[str, typer.Argument(help="Pipeline id")],
    image: Annotated[str | None, typer.Option(help="Runtime image with digest (default: placeholder)")] = None,
) -> int:
    """validate → bundle → plan → DAG + Terraform per environment → cost → docs → BUILD-MANIFEST (deterministic)."""
    from .gen.build import build

    d = _pipeline_dir(name)
    root = d.parent.parent
    out = build(load_repo(root), d, root / "build", image=image)
    return _emit(Result(command="build", ok=True, data=out))


secrets_app = typer.Typer(help="Local development secrets (never deployed; dev only).", no_args_is_help=True)
app.add_typer(secrets_app, name="secrets")


@secrets_app.command("template")
def secrets_template_cmd(
    name: Annotated[str, typer.Argument(help="Pipeline id")],
    env: Annotated[str, typer.Option(help="Environment the local run binds to")] = "dev",
    out: Annotated[Path | None, typer.Option(help="File (default ~/.factory/secrets/<team>/<env>.yaml)")] = None,
) -> int:
    """Write or extend the local secrets file with every secret name and key the pipeline needs (values empty)."""
    from .local.secrets_file import write_template

    d = _pipeline_dir(name)
    res = validate_dir(d)
    if res.load.spec is None:
        return _emit(Result(command="secrets template", ok=False, issues=res.issues))
    rep = write_template(res.load.spec, load_repo(d.parent.parent), env, out)
    return _emit(Result(command="secrets template", ok=True, data=rep))


infra_app = typer.Typer(help="Who owns the pipeline's AWS resources (create or provided by the infra team).")
app.add_typer(infra_app, name="infra")


@infra_app.command("check")
def infra_check_cmd(
    name: Annotated[str, typer.Argument(help="Pipeline id")],
    env: Annotated[str, typer.Option(help="Environment")] = "dev",
) -> int:
    """Read-only: the resources the infra team provides exist, and their roles allow what the contract needs."""
    from .gen.build import render
    from .gen.infra_check import check

    d = _pipeline_dir(name)
    r = render(load_repo(d.parent.parent), d)
    raw = r.files.get(f"infra/{env}.json")
    if raw is None:
        note = {"env": env, "provided": 0, "note": "the framework creates every resource"}
        return _emit(Result(command="infra check", ok=True, data=note))
    import boto3

    results = check(boto3.session.Session(), json.loads(raw))
    bad = [x for x in results if x["status"] not in ("ok", "unchecked")]
    issues = [Issue(code=f"infra_{x['status']}", msg=f"{x['address']} ({x['name']}): {x['detail']}") for x in bad]
    return _emit(Result(command="infra check", ok=not bad, issues=issues, data={"env": env, "results": results}))


def _json_file(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise CommandError(f"{path} must hold a JSON object", "usage")
    return doc


@app.command("run")
def run_cmd(
    name: Annotated[str, typer.Argument(help="Pipeline id")],
    env: Annotated[str, typer.Option(help="Environment the run binds to (dev only by default)")] = "dev",
    message: Annotated[Path | None, typer.Option(help="JSON message the trigger queue would deliver")] = None,
    conf: Annotated[Path | None, typer.Option(help="JSON run parameters (manual or schedule triggers)")] = None,
    task: Annotated[list[str] | None, typer.Option(help="Run only this task (repeatable)")] = None,
    skip: Annotated[list[str] | None, typer.Option(help="Skip this task, e.g. a sync you cannot run here")] = None,
    stores: Annotated[str, typer.Option(help="local: framework stores on this machine; aws: the dev stores")] = "local",
    data: Annotated[str, typer.Option(help="aws: real dev sources and destinations; local: local buckets")] = "aws",
    seed: Annotated[list[str] | None, typer.Option(help="data=local: BUCKET=FOLDER to upload first")] = None,
    secrets_file: Annotated[Path | None, typer.Option(help="Local secrets file (default per team and env)")] = None,
) -> int:
    """Run the pipeline here: every task in this process, secrets from your local file, data where you choose."""
    from .local.runner import LocalRunOptions, run_local

    if stores not in ("local", "aws") or data not in ("local", "aws"):
        raise CommandError("--stores and --data take local or aws", "usage")
    seeds = {}
    for item in seed or []:
        bucket, sep, folder = item.partition("=")
        if not sep:
            raise CommandError(f"--seed takes BUCKET=FOLDER, not '{item}'", "usage")
        seeds[bucket] = Path(folder)
    d = _pipeline_dir(name)
    root = d.parent.parent
    opts = LocalRunOptions(env=env, message=_json_file(message), conf=_json_file(conf), tasks=task, skip=skip or [],
                           stores=stores, data=data, seed=seeds, secrets_file=secrets_file)  # fmt: skip
    rep = run_local(load_repo(root), d, opts)
    log = root / ".factory" / "runs" / f"{name}-{rep['dag_run_id']}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(rep.pop("_log"), encoding="utf-8")
    rep["log"] = str(log)
    issues = [Issue(code=str(e["code"] or "task_failed"), msg=f"exit {t.get('exit_code')} in {t['task']}")
              for t in rep["tasks"] for e in [{"code": None}] if t.get("exit_code")]  # fmt: skip
    return _emit(Result(command="run", ok=rep["ok"], issues=issues, data=rep))


@app.command("verify")
def verify_cmd(name: Annotated[str, typer.Argument(help="Pipeline id")]) -> int:
    """Regenerate in memory and compare with build/<pipeline>: hand edits, stale, missing or unexpected files."""
    from .gen.verify import verify

    d = _pipeline_dir(name)
    root = d.parent.parent
    rep = verify(load_repo(root), d, root / "build")
    issues = [Issue(code=i["code"], msg=f"{i['file']}: {i['msg']}", loc=i["file"]) for i in rep.pop("issues")]
    return _emit(Result(command="verify", ok=rep["ok"], issues=issues, data=rep))


change_app = typer.Typer(help="Incremental change (ADR-0018): what a spec change does, before it ships.")
app.add_typer(change_app, name="change")


@change_app.command("plan")
def change_plan_cmd(
    name: Annotated[str, typer.Argument(help="Pipeline id")],
    base: Annotated[str, typer.Option(help="Git ref (e.g. origin/main) or a folder holding the previous pipeline")],
    record: Annotated[str | None, typer.Option(help="Write changes/<n>.json with this request text")] = None,
    tf_plan: Annotated[
        list[str] | None, typer.Option(help="ENV=FILE: `terraform show -json` of that environment's plan (repeatable)")
    ] = None,
) -> int:
    """Semantic diff, files, Terraform resources, rules to re-review, tests, DAG strategy, approvals, cost delta."""
    from .change.plan import change_plan, record_change

    d = _pipeline_dir(name)
    plans = {}
    for item in tf_plan or []:
        env, sep, file = item.partition("=")
        if not sep:
            raise CommandError(f"--tf-plan takes ENV=FILE, not '{item}'", "usage")
        plans[env] = json.loads(Path(file).read_text(encoding="utf-8"))
    out = change_plan(load_repo(d.parent.parent), d, base, plans)
    issues = [Issue(code=i["code"], msg=i["msg"]) for i in out["issues"]]
    data = out["plan"]
    if record is not None:
        if not out["ok"]:
            issues.append(Issue(code="change_not_recorded", msg="fix the issues before recording the change"))
        else:
            data = {**data, "recorded": str(record_change(d, record, out["plan"]))}
    return _emit(Result(command="change plan", ok=out["ok"], issues=issues, data=data))


agent_app = typer.Typer(
    help="Agents draft specs and custom rules; code and people decide (Phase 4).", no_args_is_help=True
)
app.add_typer(agent_app, name="agent")


def _agents() -> Any:
    try:
        from factory_agents import runner
    except ImportError:
        raise CommandError("install factory[agents] for the agent commands", "config") from None
    return runner.AgentRunner()


def _agent_result(command: str, report: Any) -> int:
    return _emit(Result(command=command, ok=report.stage in ("ready", "awaiting_acceptance"),
                        data={"stage": report.stage, "next_step": report.next_step, **report.details}))  # fmt: skip


@agent_app.command("intake")
def agent_intake(
    name: Annotated[str, typer.Argument(help="Pipeline id")],
    requirement: Annotated[Path, typer.Option(help="Requirement document (text or Markdown)")],
) -> int:
    from factory_agents.audit import AuditTrail
    from factory_agents.intake import intake, load_requirement

    d = _pipeline_dir(name)
    res = intake(_agents(), d, load_requirement(requirement), AuditTrail(d))
    data: dict[str, Any] = {"applied": res.applied, "open_questions": [q.model_dump() for q in res.open_questions]}
    if res.escalation:
        data["escalation"] = res.escalation.model_dump()
    return _emit(Result(command="agent intake", ok=res.escalation is None, data=data))


@agent_app.command("implement")
def agent_implement(
    name: Annotated[str, typer.Argument(help="Pipeline id")],
    rule_id: Annotated[str, typer.Argument(help="Rule id")],
) -> int:
    from factory_agents.audit import AuditTrail
    from factory_agents.rules import implement_rule

    d = _pipeline_dir(name)
    out = implement_rule(_agents(), d, rule_id, AuditTrail(d))
    data: dict[str, Any] = {"status": out.status, "edge_case_questions": out.edge_case_questions}
    if out.gates:
        data["gates"] = out.gates.as_dict()
    if out.review:
        data["review"] = out.review.model_dump()
    if out.escalation:
        data["escalation"] = out.escalation.model_dump()
    return _emit(Result(command="agent implement", ok=out.escalation is None, data=data))


@agent_app.command("run")
def agent_run(
    name: Annotated[str, typer.Argument(help="Pipeline id")],
    requirement: Annotated[Path | None, typer.Option(help="Requirement document; omit to continue")] = None,
) -> int:
    from factory_agents.intake import load_requirement
    from factory_agents.orchestrator import orchestrate

    d = _pipeline_dir(name)
    text = load_requirement(requirement) if requirement else None
    return _agent_result("agent run", orchestrate(_agents(), d, text))


@app.command("accept")
def accept(
    name: Annotated[str, typer.Argument(help="Pipeline id")],
    rule_id: Annotated[str, typer.Argument(help="Custom rule id")],
    by: Annotated[str, typer.Option(help="Who accepts (a person, never an agent)")],
) -> int:
    """A person accepts a reviewed custom rule: gates re-run, review checked, digest recorded in impl.lock.json."""
    try:
        from factory_agents.accept import accept_rule
        from factory_agents.errors import AgentError
    except ImportError:
        raise CommandError("install factory[agents] for `factory accept`", "config") from None
    try:
        lock = accept_rule(_pipeline_dir(name), rule_id, by)
    except AgentError as exc:
        raise CommandError(str(exc), exc.code) from None
    return _emit(Result(command="accept", ok=True, data={"rule_id": rule_id, "digest": lock["digest"],
                                                         "accepted_by": lock["accepted_by"]}))  # fmt: skip


# ---------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    STATE.json = "--json" in args
    command = next((a for a in args if not a.startswith("-")), "factory")
    try:
        rv = app(args=args, prog_name="factory", standalone_mode=False)
    except Exit as exc:
        return int(getattr(exc, "exit_code", 0) or 0)
    except Abort:
        return 2
    except TyperException as exc:  # usage errors (typer's vendored click exceptions derive from TyperException)
        message = exc.format_message() if hasattr(exc, "format_message") else str(exc)
        if STATE.json:
            env = Result(
                command=command,
                ok=False,
                issues=[Issue(code="usage", msg=message)],
                data={"error": message, "code": "usage"},
            )
            typer.echo(env.model_dump_json(indent=2))
        elif hasattr(exc, "show"):
            exc.show()
        else:
            typer.echo(message, err=True)
        return 2
    except CommandError as exc:
        issues = exc.issues or [Issue(code=exc.code, msg=str(exc))]
        _emit(Result(command=command, ok=False, issues=issues, data={"error": str(exc), "code": exc.code}))
        return 2 if exc.code in USAGE_CODES else 1
    return int(rv) if isinstance(rv, int) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
