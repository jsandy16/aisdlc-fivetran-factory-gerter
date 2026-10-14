"""Phase 6: `factory change plan` (ADR-0018), `factory verify`, the price basis and the generated docs."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from cli_helpers import PLATFORM_SETTINGS, copy_example
from factory.cli import main
from factory.gen.names import dag_id

PID = "study_pkg_delivery"


def _run(repo: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> dict[str, Any]:
    monkeypatch.chdir(repo)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["--json", *args])
    out: dict[str, Any] = json.loads(buf.getvalue())
    out["_code"] = code
    return out


def _edit(repo: Path, monkeypatch: pytest.MonkeyPatch, file: str, old: str, new: str) -> None:
    f = repo / "pipelines" / PID / "spec" / file
    text = f.read_text(encoding="utf-8")
    assert old in text, old
    f.write_text(text.replace(old, new, 1), encoding="utf-8")
    assert _run(repo, monkeypatch, "lock", PID)["_code"] == 0


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = copy_example(tmp_path / "repo")
    shutil.copytree(r / "pipelines" / PID, tmp_path / "base")  # the previous version, as a folder
    return r


def test_dag_id_versioning() -> None:
    assert dag_id("p", "0.3.1") == dag_id("p", "1.9.0") == "p"
    assert dag_id("p", "2.0.0") == "p__v2"


def test_non_breaking_change_keeps_the_dag(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _edit(repo, monkeypatch, "rules.yaml", "concurrency: 8", "concurrency: 4")
    _edit(repo, monkeypatch, "pipeline.yaml", "spec_version: 0.1.0", "spec_version: 0.2.0")
    out = _run(repo, monkeypatch, "change", "plan", PID, "--base", str(tmp_path / "base"), "--record", "slower dl")
    assert out["_code"] == 0, out["issues"]
    plan = out["data"]
    paths = {c["path"]: c for c in plan["spec_changes"]}
    assert "rules.rules[r_download].implementation.params.concurrency" in paths
    assert not plan["breaking"]
    assert plan["dag_strategy"]["strategy"] == "keep"
    assert f"dags/dev/{PID}.py" in plan["files"]["changed"]  # the bundle sha moved
    assert plan["tests_to_run"]["tasks"] == ["t1_collect"]
    assert plan["requires_approval"] == []
    rec = json.loads(Path(plan["recorded"]).read_text())
    assert rec["n"] == 1
    assert rec["request"] == "slower dl"
    assert rec["approvers"] == []
    assert rec["plan_sha256"] == plan["plan_sha256"]


def test_breaking_change_needs_major_and_gets_a_new_dag(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _edit(repo, monkeypatch, "pipeline.yaml", 'run_key: "{request_id}"', 'run_key: "{request_id}-{study_id}"')
    _edit(repo, monkeypatch, "pipeline.yaml", "spec_version: 0.1.0", "spec_version: 0.2.0")
    refused = _run(repo, monkeypatch, "change", "plan", PID, "--base", str(tmp_path / "base"), "--record", "x")
    assert refused["_code"] != 0
    codes = {i["code"] for i in refused["issues"]}
    assert {"breaking_change_needs_major", "change_not_recorded"} <= codes
    assert not (repo / "pipelines" / PID / "changes").exists()
    _edit(repo, monkeypatch, "pipeline.yaml", "spec_version: 0.2.0", "spec_version: 2.0.0")
    plan = _run(repo, monkeypatch, "change", "plan", PID, "--base", str(tmp_path / "base"))["data"]
    assert plan["breaking"]
    assert plan["dag_strategy"] == {"before": PID, "after": f"{PID}__v2", "strategy": "new_dag", "retire": PID,
                                         "how": plan["dag_strategy"]["how"]}  # fmt: skip
    assert f"dags/prod/{PID}__v2.py" in plan["files"]["added"]
    assert f"dags/prod/{PID}.py" in plan["files"]["removed"]
    assert any(t["who"] == "platform" for t in plan["requires_approval"])
    assert plan["tests_to_run"]["dag_integrity"]
    # the dispatcher starts the new DAG
    assert "aws_lambda_function.dispatcher" in plan["terraform"]["prod"]["update"]


def test_destination_change_needs_governance_and_git_base(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run([*git, *args], cwd=repo, check=True)
    _edit(repo, monkeypatch, "destinations.yaml", "prefix_pattern: \"studies/{study_id}/**\", placement: team, "
          "kms_key: alias/clinops-delivery-dev", "prefix_pattern: \"studies/{study_id}/out/**\", placement: team, "
          "kms_key: alias/clinops-delivery-dev")  # fmt: skip
    _edit(repo, monkeypatch, "pipeline.yaml", "spec_version: 0.1.0", "spec_version: 0.1.1")
    out = _run(repo, monkeypatch, "change", "plan", PID, "--base", "HEAD")
    assert out["_code"] == 0, out["issues"]
    plan = out["data"]
    assert [c["area"] for c in plan["spec_changes"] if c["area"] != "version"] == ["destinations"]
    assert [a["who"] for a in plan["requires_approval"]] == ["governance"]
    assert plan["dag_strategy"]["strategy"] == "keep"


def test_unchanged_spec_gives_an_empty_plan(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _run(repo, monkeypatch, "change", "plan", PID, "--base", str(tmp_path / "base"))["data"]
    assert plan["spec_changes"] == []
    assert plan["files"] == {"added": [], "removed": [], "changed": []}
    assert plan["cost"]["monthly_usd"]["delta"] == "0.00"


def test_verify_finds_edits_stale_missing_and_unexpected(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(repo, monkeypatch, "verify", PID)["issues"][0]["code"] == "build_missing"
    assert _run(repo, monkeypatch, "build", PID)["_code"] == 0
    assert _run(repo, monkeypatch, "verify", PID)["_code"] == 0
    out = repo / "build" / PID
    dag = out / "dags" / "dev" / f"{PID}.py"
    dag.write_text(dag.read_text() + "# hand edit\n")
    (out / "docs" / "runbook.md").unlink()
    (out / "notes.txt").write_text("x")
    codes = {(i["code"], i["loc"]) for i in _run(repo, monkeypatch, "verify", PID)["issues"]}
    assert codes == {("generated_file_edited", f"dags/dev/{PID}.py"), ("generated_file_missing", "docs/runbook.md"),
                     ("generated_file_unexpected", "notes.txt")}  # fmt: skip
    assert _run(repo, monkeypatch, "build", PID)["_code"] == 0
    _edit(repo, monkeypatch, "rules.yaml", "concurrency: 8", "concurrency: 4")  # spec changed, not rebuilt
    stale = _run(repo, monkeypatch, "verify", PID)
    assert stale["_code"] != 0
    assert {i["code"] for i in stale["issues"]} == {"build_stale"}


def test_calibrated_prices_are_labelled_and_never_mixed(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    placeholder = _run(repo, monkeypatch, "cost", PID)["data"]
    assert placeholder["basis"] == "placeholder"
    assert placeholder["note"].startswith("indicative")
    settings = tmp_path / "platform-settings"
    shutil.copytree(PLATFORM_SETTINGS, settings)
    from factory_spec.catalog import pricing

    doc = pricing().model_dump(mode="json")
    (settings / "pricing.json").write_text(json.dumps(doc))
    toml = (repo / "factory.toml").read_text().replace(PLATFORM_SETTINGS.as_posix(), settings.as_posix())
    (repo / "factory.toml").write_text(toml)
    refused = _run(repo, monkeypatch, "cost", PID)
    assert refused["_code"] != 0
    assert "status: calibrated" in json.dumps(refused)
    doc |= {"status": "calibrated", "source": "CUR 2026-09 (synthetic test figures)",
            "mwaa_share_per_pipeline_month": "40"}  # fmt: skip
    (settings / "pricing.json").write_text(json.dumps(doc))
    cal = _run(repo, monkeypatch, "cost", PID)["data"]
    assert cal["basis"] == "calibrated"
    assert cal["note"].startswith("calibrated: CUR")
    assert cal["mwaa_share_usd"] == "40.00"
    assert cal["monthly_usd"] != placeholder["monthly_usd"]
    build = _run(repo, monkeypatch, "build", PID)
    manifest = json.loads((Path(build["data"]["out"]) / "BUILD-MANIFEST.json").read_text())
    assert manifest["pricing"]["basis"] == "calibrated"


def test_generated_docs(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = Path(_run(repo, monkeypatch, "build", PID)["data"]["out"]) / "docs"
    runbook, lineage, alerts = ((out / n).read_text() for n in ("runbook.md", "lineage.md", "alerts.md"))
    assert "factory-clinops-data-study-pkg-delivery-t1-collect" in runbook
    assert "| security | 77 |" in runbook
    assert "`portal_client`" in runbook
    assert "r_zip -- package --> r_checksum" in lineage
    assert "r_upload_x --> slot_x" in lineage
    assert "| data | 65 | fails at once (not retried) | `clinops_data` |" in alerts
    for text in (runbook, lineage, alerts):
        assert "SHA256:" not in text  # no host keys, no secret material: ids and names only


def test_terraform_plan_json_drives_the_infrastructure_delta(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 8: with a real `terraform show -json`, replacements are visible and stateful ones need approval."""
    plan_json = {"resource_changes": [
        {"address": "aws_lambda_function.t1_collect", "change": {"actions": ["update"]}},
        {"address": "aws_sqs_queue.dlq", "change": {"actions": ["delete", "create"]}},
        {"address": "aws_iam_role.t9", "change": {"actions": ["create"]}},
        {"address": "aws_iam_role.old", "change": {"actions": ["delete"]}},
        {"address": "data.aws_caller_identity.me", "change": {"actions": ["read"]}},
        {"address": "aws_s3_object.x", "change": {"actions": ["no-op"]}},
    ]}  # fmt: skip
    f = tmp_path / "dev-plan.json"
    f.write_text(json.dumps(plan_json))
    plan = _run(repo, monkeypatch, "change", "plan", PID, "--base", str(tmp_path / "base"), "--tf-plan", f"dev={f}")
    assert plan["data"]["terraform"]["dev"] == {
        "create": ["aws_iam_role.t9"], "update": ["aws_lambda_function.t1_collect"],
        "replace": ["aws_sqs_queue.dlq"], "destroy": ["aws_iam_role.old"], "source": ["terraform plan"]}  # fmt: skip
    assert {"who": "platform", "why": "replaces stateful aws_sqs_queue.dlq in dev"} in plan["data"]["requires_approval"]
    bad = _run(repo, monkeypatch, "change", "plan", PID, "--base", str(tmp_path / "base"), "--tf-plan", "dev")
    assert bad["_code"] != 0
