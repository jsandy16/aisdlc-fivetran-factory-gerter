"""`factory build` is deterministic and complete; `factory cost`, `bundle` and `test` run on the worked example."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cli_helpers import copy_example
from factory.cli import main


def _run(repo: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> dict:
    monkeypatch.chdir(repo)
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["--json", *args])
    out = json.loads(buf.getvalue())
    out["_code"] = code
    return out


def test_build_is_byte_identical_and_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = copy_example(tmp_path / "repo")
    first = _run(repo, monkeypatch, "build", "study_pkg_delivery")
    assert first["_code"] == 0
    out = Path(first["data"]["out"])
    manifest = json.loads((out / "BUILD-MANIFEST.json").read_text())
    for rel, sha in manifest["files"].items():
        assert hashlib.sha256((out / rel).read_bytes()).hexdigest() == sha
    assert {f"dags/{e}/study_pkg_delivery.py" for e in manifest["environments"]} <= set(manifest["files"])
    assert {f"terraform/{e}/main.tf.json" for e in manifest["environments"]} <= set(manifest["files"])
    pod_task = (out / "dags" / "dev" / "study_pkg_delivery.py").read_text().split("t3_upload = ", 1)[1]
    assert "env_vars={" in pod_task  # pods get the same FACTORY_* settings Terraform gives the Lambdas
    assert '"FACTORY_PIPELINE_ID": "study_pkg_delivery"' in pod_task
    second = _run(repo, monkeypatch, "build", "study_pkg_delivery")
    assert second["data"]["manifest_sha256"] == first["data"]["manifest_sha256"]


def test_cost_bundle_and_owner_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = copy_example(tmp_path / "repo")
    cost = _run(repo, monkeypatch, "cost", "study_pkg_delivery")
    assert cost["_code"] == 0
    assert cost["data"]["within_budget"]
    assert [t["engine"] for t in cost["data"]["tasks"]] == ["lambda", "lambda", "eks_pod"]
    b1 = _run(repo, monkeypatch, "bundle", "study_pkg_delivery")
    b2 = _run(repo, monkeypatch, "bundle", "study_pkg_delivery")
    assert b1["data"]["sha256"] == b2["data"]["sha256"]
    tests = _run(repo, monkeypatch, "test", "study_pkg_delivery")
    assert tests["_code"] == 0
    assert tests["data"]["fail"] == 0
    assert tests["data"]["pass"] == 5
