"""`pf` command line. Every command accepts --json and then prints exactly one Result envelope (see envelope.py).

Exit codes: 0 ok · 1 the command ran but found problems · 2 usage or environment error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from .envelope import Issue, PfError, Result


def _root() -> Path:
    from .bundle import find_repo_root

    return find_repo_root()


def _bundle(name: str):
    from .bundle import load_bundle, pipeline_dir

    return load_bundle(pipeline_dir(name, _root()))


# ---------------------------------------------------------------------------- commands
def cmd_init_repo(a) -> Result:
    root = Path(a.path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "pf.toml").write_text('# pipeline-factory repository marker\nspec_api_version = "pf/v1"\n', encoding="utf-8") if not (root / "pf.toml").exists() else None
    (root / "pipelines").mkdir(exist_ok=True)
    gi = root / ".gitignore"
    lines = ["artifacts/", "**/build/connector/files/", "**/build/connector/configuration.json", "configuration.json", ".venv/", "__pycache__/", "*.pyc"]
    existing = gi.read_text().splitlines() if gi.exists() else []
    gi.write_text("\n".join(existing + [x for x in lines if x not in existing]) + "\n", encoding="utf-8")
    return Result(command="init-repo", ok=True, data={"root": str(root)})


def cmd_new(a) -> Result:
    from .scaffold import new_pipeline

    root = _root()
    ents = [e.strip() for e in a.entities.split(",") if e.strip()] if a.entities else [a.name]
    envs = [e.strip() for e in a.envs.split(",") if e.strip()]
    path = new_pipeline(root, a.name, a.source, a.destination, ents, envs, force=a.force)
    data: dict[str, Any] = {"path": str(path.relative_to(root)), "files": sorted(p.name for p in path.glob("*.yaml"))}
    if a.requirements:
        from .intake import run_intake

        data["intake"] = run_intake(path, Path(a.requirements).resolve())
    from .bundle import load_bundle

    b = load_bundle(path)
    data["to_fill"] = len([i for i in b.issues if i.code == "required"])
    return Result(command="new", ok=True, data=data, issues=[i for i in b.issues if i.code != "required"])


def cmd_intake(a) -> Result:
    from .bundle import pipeline_dir
    from .intake import run_intake

    path = pipeline_dir(a.name, _root())
    data = run_intake(path, Path(a.document).resolve(), overwrite=a.overwrite)
    return Result(command="intake", ok=True, data=data)


def cmd_validate(a) -> Result:
    b = _bundle(a.name)
    issues = list(b.issues)
    errors = [i for i in issues if i.severity == "error" and not (a.allow_unfilled and i.code == "required")]
    if b.ok:
        from .lock import english_status
        from .rulecompiler import load_rules_lock

        issues += [i.model_copy(update={"severity": "warning"}) for i in english_status(b, load_rules_lock(b.path))]
    return Result(command="validate", ok=not errors, issues=issues, data={"unfilled": len([i for i in issues if i.code == "required"])})


def agent_session() -> bool:
    """True inside Claude Code / Agent SDK sessions; person-only actions are refused there."""
    return bool(os.environ.get("CLAUDECODE") or os.environ.get("PF_AGENT_SESSION"))


def cmd_edit(a) -> Result:
    from .bundle import pipeline_dir
    from .edit import apply_edit, check_path

    path = pipeline_dir(a.name, _root())
    if a.cmd == "add-check":
        file, loc = check_path(a.entity, a.attribute, a.package, a.row)
        new = apply_edit(path, file, loc, "add", a.value, keep_invalid=a.keep_invalid)
    else:
        op = {"set": "set", "add": "add", "remove": "remove"}[a.cmd]
        new = apply_edit(path, a.file, a.path, op, getattr(a, "value", None), keep_invalid=a.keep_invalid)
    return Result(command=a.cmd, ok=not new, issues=new)


def cmd_catalog(a) -> Result:
    from .catalog import catalog

    return Result(command="catalog", ok=True, data=catalog())


def cmd_schema(a) -> Result:
    from .bundle import SPEC_FILES

    out = {f: m.model_json_schema() for f, m in SPEC_FILES.items()}
    if a.write:
        d = Path(a.write)
        d.mkdir(parents=True, exist_ok=True)
        for f, s in out.items():
            (d / f.replace(".yaml", ".schema.json")).write_text(json.dumps(s, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return Result(command="schema", ok=True, data={"files": sorted(out)} if a.write else out)


def cmd_rules(a) -> Result:
    from .rulecompiler import accept_rule, compile_rules, english_rules, load_rules_lock

    b = _bundle(a.name)
    if a.action == "list":
        rl = load_rules_lock(b.path)
        data = {r.rule_id: {"scope": r.scope, "where": f"{r.file}:{r.loc}",
                            "status": (rl.rules[r.rule_id].status if rl and r.rule_id in rl.rules else "not_compiled")} for r in english_rules(b)}
        return Result(command="rules list", ok=True, data=data)
    if not b.ok:
        raise PfError("fix validation errors before compiling rules", "invalid", [i for i in b.issues if i.severity == "error"])
    if a.action == "compile":
        rl, issues = compile_rules(b, only=a.rule or None, log_dir=b.path / "intake")
        return Result(command="rules compile", ok=not any(i.severity == "error" for i in issues), issues=issues,
                      data={rid: {"status": e.status, "verification": e.verification, "expression": e.expression,
                                  "back_translation": e.back_translation, "ambiguities": e.ambiguities} for rid, e in rl.rules.items()})
    if a.action == "accept":
        if agent_session():
            raise PfError("accepting a rule is a person's decision; agents cannot run it", "forbidden")
        e = accept_rule(b, a.rule[0], a.by)
        return Result(command="rules accept", ok=True, data={"rule": a.rule[0], "accepted_by": e.accepted_by})
    raise PfError("unknown rules action", "usage")


def cmd_build(a) -> Result:
    from .generate import check, render_all, write

    root = _root()
    b = _bundle(a.name)
    files = render_all(b, root)
    if a.check:
        diffs = check(root, files)
        return Result(command="build --check", ok=not diffs, issues=[Issue(msg=d, code="stale_build") for d in diffs],
                      data={"files": len(files)})
    written = write(root, files, a.name)
    return Result(command="build", ok=True, data={"files": len(written), "build": f"pipelines/{a.name}/build"})


def cmd_test(a) -> Result:
    from .testing import run_tests

    res = run_tests(_root(), _bundle(a.name))
    return Result(command="test", ok=res["report"]["ok"], issues=res["issues"],
                  data={"cases": [c["case"] for c in res["report"]["cases"]], "uncovered": res["report"]["uncovered"]})


def cmd_release(a) -> Result:
    from .release import release

    data = release(_root(), _bundle(a.name), upload=a.upload, check_only=a.check)
    return Result(command="release", ok=True, data=data)


def cmd_deploy(a) -> Result:
    from .deploy import deploy

    if agent_session() and not a.dry_run:
        raise PfError("deploying is done by CI after review; agents may only use --dry-run", "forbidden")
    return Result(command="deploy", ok=True, data=deploy(_root(), _bundle(a.name), a.env, dry_run=a.dry_run))


def cmd_fivetran(a) -> Result:
    from .fivetran_api import FivetranApi, drift, ensure_destination

    root = _root()
    b = _bundle(a.name)
    settings = json.loads((root / "pipelines" / a.name / "build" / "fivetran" / f"{a.env}.json").read_text())
    if a.action == "ensure-destination":
        if agent_session() and not a.dry_run:
            raise PfError("creating Fivetran destinations is a platform action; agents may only use --dry-run", "forbidden")
        api = FivetranApi() if not a.dry_run else _DryApi()
        return Result(command="fivetran ensure-destination", ok=True, data=ensure_destination(api, settings, dry_run=a.dry_run))
    sched = b.pipeline.schedule[a.env].model_dump() if b.pipeline and a.env in b.pipeline.schedule else {"sync_frequency": 360, "paused": False}
    d = drift(FivetranApi(), settings["destination"]["group_name"], a.name, sched)
    return Result(command="fivetran drift", ok=d["status"] == "in_sync", data=d)


class _DryApi:
    def find_group(self, name):
        return None


def cmd_dq(a) -> Result:
    from .dq import run

    rep = run(_root(), a.name, a.engine, db=a.db, schema=a.schema, workgroup=a.workgroup, output=a.output,
              certify=a.certify, evidence_bucket=a.evidence_bucket)
    return Result(command="dq run", ok=rep["certified"], data=rep)


def cmd_status(a) -> Result:
    from .status import status

    root = _root()
    names = [a.name] if a.name else sorted(p.name for p in (root / "pipelines").iterdir() if p.is_dir())
    return Result(command="status", ok=True, data={"pipelines": [status(root, n) for n in names]})


def cmd_hook(a) -> Result:
    from .hooks import run_hook

    code = run_hook(a.event)
    sys.exit(code)


# ---------------------------------------------------------------------------- parser
def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pf", description="pipeline-factory: spec-driven Fivetran pipelines (AI-SDLC)")
    p.add_argument("--json", action="store_true", help="print one JSON Result envelope")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init-repo", help="make a folder a pipeline-factory repository")
    s.add_argument("path", nargs="?", default=".")
    s.set_defaults(fn=cmd_init_repo)

    s = sub.add_parser("new", help="generate the fill-in spec bundle for a source and destination type")
    s.add_argument("name")
    s.add_argument("--source", required=True, choices=["sftp", "ftp", "rds"])
    s.add_argument("--destination", required=True, choices=["s3"])
    s.add_argument("--entities", help="comma-separated target table names")
    s.add_argument("--envs", default="dev,prod")
    s.add_argument("--requirements", help="requirement document (md, txt, docx, pdf) to draft values from")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("intake", help="draft spec values from a requirement document (LLM step)")
    s.add_argument("name")
    s.add_argument("document")
    s.add_argument("--overwrite", action="store_true", help="also replace values a person already filled")
    s.set_defaults(fn=cmd_intake)

    s = sub.add_parser("validate", help="validate the spec bundle (deterministic)")
    s.add_argument("name")
    s.add_argument("--allow-unfilled", action="store_true", help="do not fail on <REQUIRED> placeholders")
    s.set_defaults(fn=cmd_validate)

    for c, h in (("set", "set a value"), ("add", "append to a list"), ("remove", "remove a key or list item")):
        s = sub.add_parser(c, help=f"{h} in a spec file (keeps comments; rolls back invalid edits)")
        s.add_argument("name")
        s.add_argument("file", help="e.g. source.yaml")
        s.add_argument("path", help="dotted path; select list items by name: entities[lab].attributes[visit_date].checks")
        if c != "remove":
            s.add_argument("value", help="YAML value, e.g. '{id: x, kind: not_null, description: ...}'")
        s.add_argument("--keep-invalid", action="store_true")
        s.set_defaults(fn=cmd_edit)

    s = sub.add_parser("add-check", help="add a check at package, entity, row or attribute scope")
    s.add_argument("name")
    s.add_argument("value", help="YAML check, e.g. '{id: dob_past, kind: expression, expr: \"value < \\\"2025-01-01\\\"\", description: ...}'")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--package", action="store_true")
    g.add_argument("--row", action="store_true")
    s.add_argument("--entity")
    s.add_argument("--attribute")
    s.add_argument("--keep-invalid", action="store_true")
    s.set_defaults(fn=cmd_edit)

    s = sub.add_parser("catalog", help="list source types, checks and transforms with their parameters")
    s.set_defaults(fn=cmd_catalog)

    s = sub.add_parser("schema", help="JSON Schema of every spec file")
    s.add_argument("--write", help="folder to write *.schema.json into")
    s.set_defaults(fn=cmd_schema)

    s = sub.add_parser("rules", help="English rules: compile (LLM, build time), accept (person), list")
    s.add_argument("action", choices=["compile", "accept", "list"])
    s.add_argument("name")
    s.add_argument("rule", nargs="*")
    s.add_argument("--by", help="GitHub handle of the person accepting")
    s.set_defaults(fn=cmd_rules)

    s = sub.add_parser("build", help="generate connector, checks, Terraform, docs and CI (deterministic)")
    s.add_argument("name")
    s.add_argument("--check", action="store_true", help="fail if generated files differ from the specs")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("test", help="break-and-verify on fixtures with the real runtime")
    s.add_argument("name")
    s.set_defaults(fn=cmd_test)

    s = sub.add_parser("release", help="create (or check) the immutable versioned artefact")
    s.add_argument("name")
    s.add_argument("--check", action="store_true", help="CI: verify version bump and committed release record")
    s.add_argument("--upload", help="s3://bucket[/prefix] (write-once)")
    s.set_defaults(fn=cmd_release)

    s = sub.add_parser("deploy", help="deploy to Fivetran (CI) and apply the schedule")
    s.add_argument("name")
    s.add_argument("--env", required=True)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_deploy)

    s = sub.add_parser("fivetran", help="destination setup and drift checks")
    s.add_argument("action", choices=["ensure-destination", "drift"])
    s.add_argument("name")
    s.add_argument("--env", required=True)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_fivetran)

    s = sub.add_parser("dq", help="post-load checks (athena or duckdb) and certification")
    s.add_argument("action", choices=["run"])
    s.add_argument("name")
    s.add_argument("--engine", choices=["athena", "duckdb"], default="athena")
    s.add_argument("--db", help="duckdb file (dry runs)")
    s.add_argument("--schema", help="override schema name (e.g. the local tester's schema)")
    s.add_argument("--workgroup", default="primary")
    s.add_argument("--output", help="Athena query result location s3://...")
    s.add_argument("--certify", action="store_true")
    s.add_argument("--evidence-bucket")
    s.set_defaults(fn=cmd_dq)

    s = sub.add_parser("status", help="ADLC stage and next action for each pipeline")
    s.add_argument("name", nargs="?")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("hook", help="Claude Code hook entry point (reads the hook JSON on stdin)")
    s.add_argument("event", choices=["pre-tool", "post-edit"])
    s.set_defaults(fn=cmd_hook)
    return p


def _print_human(r: Result) -> None:
    mark = "OK" if r.ok else "PROBLEMS"
    print(f"{r.command}: {mark}")
    for i in r.issues:
        where = f"{i.file}{(':' + i.loc) if i.loc else ''}"
        print(f"  {i.severity:7} {where}: {i.msg}")
    if r.data:
        print(json.dumps(r.data, indent=2, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    p = parser()
    a = p.parse_args(argv)
    fn: Callable[[Any], Result] = a.fn
    try:
        r = fn(a)
        code = 0 if r.ok else 1
    except PfError as exc:
        r = Result(command=a.cmd, ok=False, issues=exc.issues or [Issue(msg=str(exc), code=exc.code)],
                   data={"error": str(exc), "code": exc.code})
        code = 2 if exc.code in ("usage", "no_repo", "missing_dependency", "config") else 1
    if a.json:
        print(r.model_dump_json(indent=2))
    else:
        _print_human(r)
        if not r.ok and r.data.get("error") and not r.issues:
            print(f"  error: {r.data['error']}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
