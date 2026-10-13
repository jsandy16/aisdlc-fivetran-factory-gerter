"""Build determinism and dependency safety, release versioning, deploy guards."""
from __future__ import annotations

import json

import pytest

from pf.bundle import load_bundle
from pf.envelope import PfError
from pf.generate import check, render_all, write


def test_build_is_deterministic(repo, onc):
    b = load_bundle(onc)
    a, c = render_all(b, repo), render_all(load_bundle(onc), repo)
    assert a == c
    write(repo, a, "oncology_labs")
    assert check(repo, render_all(load_bundle(onc), repo)) == []


def test_requirements_are_normalised_and_complete(repo, onc):
    files = render_all(load_bundle(onc), repo)
    req = files["pipelines/oncology_labs/build/connector/requirements.txt"].decode().split()
    assert all(r == r.lower() and "-" not in r.split("==")[0] for r in req)  # what the SDK compares literally
    assert {"cel_python==0.5.0", "paramiko==5.0.0", "boto3==1.43.111", "botocore==1.43.111"} <= set(req)
    assert not any(r.startswith(("fivetran", "requests")) for r in req)
    vendored = [k for k in files if "/pf_runtime/sources/" in k]
    assert sorted(k.rsplit("/", 1)[1] for k in vendored) == ["__init__.py", "sftp.py"]  # only what this source runs


def test_celpy_is_hidden_from_pipreqs():
    import ast
    from pathlib import Path

    import pf_runtime

    tree = ast.parse((Path(pf_runtime.__file__).parent / "cel.py").read_text())
    static = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    static |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(m and m.startswith("celpy") for m in static)


@pytest.mark.parametrize("code,ok", [("import json\n", True), ("import requests_toolbelt\n", False),
                                     ("import importlib\nm = importlib.import_module(name)\n", False),
                                     ("import psycopg2\n", False)])
def test_custom_rule_imports_are_checked(repo, onc, code, ok):
    (onc / "rules").mkdir(exist_ok=True)
    (onc / "rules" / "extra.py").write_text(code)
    if ok:
        render_all(load_bundle(onc), repo)
    else:
        with pytest.raises(PfError) as e:
            render_all(load_bundle(onc), repo)
        assert e.value.code == "dependencies"


def test_release_classification(onc):
    import copy

    from pf.lock import build_lock
    from pf.release import classify

    base = build_lock(load_bundle(onc))
    same = copy.deepcopy(base)
    more = copy.deepcopy(base)
    more["entities"][0]["attributes"].append({**more["entities"][0]["attributes"][-1], "name": "extra_col", "nullable": True})
    fewer = copy.deepcopy(base)
    fewer["entities"][0]["attributes"].pop()
    rekey = copy.deepcopy(base)
    rekey["entities"][0]["primary_key"] = ["subject_key"]
    assert classify(base, same)[0] in ("none", "patch")
    assert classify(base, more)[0] == "minor"
    assert classify(base, fewer)[0] == "major" and classify(base, rekey)[0] == "major"


def _built(repo, onc):
    write(repo, render_all(load_bundle(onc), repo), "oncology_labs")
    return load_bundle(onc)


def test_deploy_needs_every_secret(repo, onc, monkeypatch):
    from pf.deploy import configuration

    b = _built(repo, onc)
    lock = json.loads((onc / "build" / "spec.lock.json").read_text())
    with pytest.raises(PfError):
        configuration(lock, "dev", environ={})
    env = {f"PF_SECRET_{k.upper()}": "v-" + k for k in lock["secrets"]["dev"]}
    cfg = configuration(lock, "dev", environ=env)
    assert cfg["pf_environment"] == "dev" and not any(k.endswith("_prod") for k in cfg)
    del b


def test_deploy_refuses_unreleased_build(repo, onc, monkeypatch):
    import shutil

    from pf.deploy import deploy

    b = _built(repo, onc)
    lock = json.loads((onc / "build" / "spec.lock.json").read_text())
    for k in lock["secrets"]["dev"]:
        monkeypatch.setenv(f"PF_SECRET_{k.upper()}", "value")
    monkeypatch.setenv("FIVETRAN_API_KEY", "a2V5OnNlY3JldA==")
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/fivetran")
    with pytest.raises(PfError) as e:
        deploy(repo, b, "dev")
    assert e.value.code == "not_released"


def test_secrets_are_masked_including_pem_lines():
    from pf.deploy import _mask

    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU=\nAAAAAAAAAAEAAAAzAAAAC3NzaC1lZDI1NTE5\n-----END OPENSSH PRIVATE KEY-----\n"
    out = _mask(f"error near b3BlbnNzaC1rZXktdjEAAAAABG5vbmU= and salt=s3cr3t-salt", {"k": pem, "salt": "s3cr3t-salt", "env": "dev"})
    assert "b3BlbnNz" not in out and "s3cr3t" not in out


def test_agent_sessions_cannot_accept_or_deploy(repo, monkeypatch, capsys):
    from pf.cli import main

    monkeypatch.setenv("PF_AGENT_SESSION", "1")
    for argv in (["--json", "rules", "accept", "oncology_labs", "visit_before_receipt", "--by", "@x"],
                 ["--json", "deploy", "oncology_labs", "--env", "dev"],
                 ["--json", "fivetran", "ensure-destination", "oncology_labs", "--env", "dev"]):
        rc = main(argv)
        out = json.loads(capsys.readouterr().out)
        assert rc != 0 and out["issues"][0]["code"] == "forbidden", argv
