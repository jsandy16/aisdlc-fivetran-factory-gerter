"""`pf status` — where a pipeline stands on the ADLC spine, worked out from files every time (no hidden state).

Learn -> Plan -> Validate -> Build -> Verify -> Deploy -> Operate. Each stage names its next action and who
owns the transition; skills and agents route on this, never on memory.
"""
from __future__ import annotations

import json
from pathlib import Path

from .bundle import load_bundle
from .lock import english_status, spec_sha
from .rulecompiler import load_rules_lock


def status(root: Path, name: str) -> dict:
    path = root / "pipelines" / name
    b = load_bundle(path)
    req_holes = [i for i in b.issues if i.code == "required" and i.file == "pipeline.yaml" and i.loc.startswith("requirement")]
    holes = [i for i in b.issues if i.code == "required"]
    errors = [i for i in b.issues if i.severity == "error" and i.code != "required"]

    def out(stage: str, nxt: str, owner: str, **extra) -> dict:
        return {"pipeline": name, "stage": stage, "next": nxt, "owner": owner, **extra}

    if req_holes:
        return out("learn", "Describe the requirement in pipeline.yaml (or `pf intake` a requirement document)", "product owner",
                   missing=[f"{i.file}:{i.loc}" for i in req_holes])
    if holes:
        by_file: dict[str, int] = {}
        for i in holes:
            by_file[i.file] = by_file.get(i.file, 0) + 1
        return out("plan", "Fill every <REQUIRED> value; each owner fills the file they own", "file owners", unfilled=by_file)
    if errors:
        return out("validate", "Fix validation errors (`pf validate`)", "data engineer", errors=[i.model_dump() for i in errors[:20]])
    rules = english_status(b, load_rules_lock(path))
    if rules:
        return out("validate", "Compile English rules (`pf rules compile`) and have their owners accept them", "rule owners",
                   rules=[i.msg for i in rules])
    build = path / "build"
    man = build / "BUILD-MANIFEST.json"
    if not man.exists() or json.loads(man.read_text())["spec_sha256"] != spec_sha(b):
        return out("build", f"Run `pf build {name}`", "data engineer")
    rep = build / "TEST-REPORT.json"
    if not rep.exists():
        return out("verify", f"Add fixtures and run `pf test {name}`", "data engineer + quality")
    r = json.loads(rep.read_text())
    if r.get("spec_sha256") != spec_sha(b) or not r.get("ok"):
        return out("verify", f"Run `pf test {name}` until it passes", "data engineer + quality", uncovered=r.get("uncovered"))
    version = b.pipeline.version if b.pipeline else "?"
    if not (path / "releases" / f"{version}.json").exists():
        return out("deploy", f"Run `pf release {name}`, commit, open a pull request; CI deploys after review", "data engineer, then reviewers")
    return out("operate", "Released. Watch pf_* evidence and `pf dq run`; changes start a new version", "data owner", version=version)
