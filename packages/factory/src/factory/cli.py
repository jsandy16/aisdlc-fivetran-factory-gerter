"""`factory`: the Pipeline Factory command line.

Every command accepts `--json` (before the command name) and then prints exactly one Result envelope, including on
usage errors. Exit codes: 0 ok · 1 the command ran and found problems · 2 usage or environment error.
"""

from __future__ import annotations

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
) -> int:
    root = path.resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER
    if not marker.exists():
        lines = ["# Pipeline Factory team repository", 'api_version = "factory/v1"']
        if team:
            lines.append(f'team = "{team}"')
        lines.append('environments = ["dev", "test", "ppt", "prod"]')
        if platform_settings:
            lines.append(f'platform_settings = "{platform_settings}"')
        marker.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "pipelines").mkdir(exist_ok=True)
    gi = root / ".gitignore"
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    wanted = [".venv/", "__pycache__/", "*.pyc", ".factory/"]
    gi.write_text("\n".join(existing + [w for w in wanted if w not in existing]) + "\n", encoding="utf-8")
    return _emit(Result(command="init", ok=True, data={"root": str(root)}))


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
