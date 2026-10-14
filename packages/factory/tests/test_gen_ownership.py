"""Change request M2: the framework creates the pipeline's AWS resources, or uses ones the infra team provides."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from cli_helpers import copy_example
from factory.cli import main
from factory.gen.dag import generate_dag
from factory.gen.infra_check import check
from factory.gen.names import deploy_for
from factory.gen.terraform import generate_stack
from factory_core.envelope import CommandError
from factory_spec.load import load_spec
from factory_spec.repo import load_repo
from factory_spec.sections.environments import Infrastructure

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "team-pipelines"
PID = "study_pkg_delivery"


def _stack(infra: dict[str, Any] | None = None, env: str = "dev") -> tuple[dict[str, Any], dict[str, Any], Any]:
    repo = load_repo(EXAMPLE)
    spec = load_spec(EXAMPLE / "pipelines" / PID).spec
    assert spec is not None
    if infra is not None:
        envs = spec.environments.environments
        envs[env] = envs[env].model_copy(update={"infrastructure": Infrastructure.model_validate(infra)})
    platform = repo.platform[env]
    files, contract = generate_stack(spec, platform, deploy_for(platform, "clinops_data", PID, "a" * 64))
    return files, contract, spec


def test_default_creates_everything_as_before() -> None:
    files, contract, _ = _stack()
    assert set(files) == {"providers.tf.json", "main.tf.json"}
    assert "data" not in files["main.tf.json"]
    assert contract["provide"] == []
    assert set(files["main.tf.json"]["resource"]) >= {"aws_sqs_queue", "aws_iam_role", "aws_lambda_function"}


def test_existing_queues_become_lookups_and_a_handover() -> None:
    files, contract, _ = _stack({"queues": {"mode": "existing", "names": {"trigger": "infra-made-queue"}}})
    m = files["main.tf.json"]
    assert "aws_sqs_queue" not in m["resource"]
    assert m["data"]["aws_sqs_queue"] == {"dlq": {"name": "clinops-package-ready-dev-dlq"},
                                          "trigger": {"name": "infra-made-queue"}}  # fmt: skip
    esm = m["resource"]["aws_lambda_event_source_mapping"]["trigger"]
    assert esm["event_source_arn"] == "${data.aws_sqs_queue.trigger.arn}"
    assert m["resource"]["aws_lambda_function"]["dispatcher"]["dead_letter_config"] == {
        "target_arn": "${data.aws_sqs_queue.dlq.arn}"
    }
    assert {r["from"] for r in files["ownership.tf.json"]["removed"]} == {"aws_sqs_queue.dlq", "aws_sqs_queue.trigger"}
    provided = {p["address"]: p for p in contract["provide"]}
    assert provided["aws_sqs_queue.trigger"]["name"] == "infra-made-queue"
    redrive = json.loads(provided["aws_sqs_queue.trigger"]["settings"]["redrive_policy"])
    assert redrive["maxReceiveCount"] == 5  # the infra team gets the exact settings the framework would use
    assert redrive["deadLetterTargetArn"] == "arn:aws:sqs:eu-west-1:200000000001:clinops-package-ready-dev-dlq"
    assert provided["aws_sqs_queue.dlq"]["settings"]["kms_master_key_id"] == "<kms_key_arn: from the platform settings>"


def test_everything_existing_leaves_only_lookups_and_uses_their_names() -> None:
    names = {"t1_collect": "team-made-t1", "t2_checksum": "team-made-t2", "dispatcher": "team-made-dispatch"}
    files, contract, spec = _stack({"mode": "existing", "compute": {"mode": "existing", "names": names}})
    assert "resource" not in files["main.tf.json"]
    assert set(files["main.tf.json"]["data"]) == {"aws_iam_role", "aws_sqs_queue", "aws_cloudwatch_log_group",
                                                  "aws_lambda_function"}  # fmt: skip
    groups = {p["group"] for p in contract["provide"]}
    assert groups == {"roles", "queues", "logs", "compute"}
    policies = [p for p in contract["provide"] if p["type"] == "aws_iam_role_policy"]
    expected = {f"aws_iam_role_policy.{k}" for k in ("t1_collect", "t2_checksum", "t3_upload", "dispatcher")}
    assert {p["address"] for p in policies} == expected
    platform = load_repo(EXAMPLE).platform["dev"]
    dag = generate_dag(spec, deploy_for(platform, "clinops_data", PID, "a" * 64), "e" * 64, function_names=names)
    assert "function_name='team-made-t1'" in dag


def test_unknown_name_override_is_refused() -> None:
    with pytest.raises(CommandError, match="no such resource key"):
        _stack({"queues": {"mode": "existing", "names": {"nope": "x"}}})


def test_adopt_imports_what_already_exists() -> None:
    files, contract, _ = _stack({"roles": {"adopt": True}, "compute": {"adopt": True}})
    imports = {i["to"]: i["id"] for i in files["ownership.tf.json"]["import"]}
    assert imports["aws_iam_role.t1_collect"] == "factory-clinops-data-study-pkg-delivery-t1-collect"
    assert imports["aws_iam_role_policy.t1_collect"] == "factory-clinops-data-study-pkg-delivery-t1-collect:runtime"
    assert imports["aws_lambda_function.dispatcher"] == "factory-clinops-data-study-pkg-delivery-dispatch"
    assert "aws_lambda_event_source_mapping.trigger" in contract["adopt_needs_import_ids"]


def _cli(repo: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> dict[str, Any]:
    monkeypatch.chdir(repo)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["--json", *args])
    out: dict[str, Any] = json.loads(buf.getvalue())
    out["_code"] = code
    return out


def test_handing_queues_to_the_infra_team_is_a_release_not_a_destroy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = copy_example(tmp_path / "repo")
    shutil.copytree(repo / "pipelines" / PID, tmp_path / "base")
    spec_dir = repo / "pipelines" / PID / "spec"
    env_file = spec_dir / "environments.yaml"
    portal = '    portal: {base_url: "https://portal.dev.acme.example"}\n'
    env_file.write_text(
        env_file.read_text().replace(portal, portal + "    infrastructure: {queues: {mode: existing}}\n")
    )
    pipe = spec_dir / "pipeline.yaml"
    pipe.write_text(pipe.read_text().replace("spec_version: 0.1.0", "spec_version: 0.1.1"))
    assert _cli(repo, monkeypatch, "lock", PID)["_code"] == 0
    plan = _cli(repo, monkeypatch, "change", "plan", PID, "--base", str(tmp_path / "base"))
    assert plan["_code"] == 0, plan["issues"]
    dev = plan["data"]["terraform"]["dev"]
    assert dev["release"] == ["aws_sqs_queue.dlq", "aws_sqs_queue.trigger"]
    assert dev["destroy"] == []
    assert not any("destroys stateful" in a["why"] for a in plan["data"]["requires_approval"])
    build = _cli(repo, monkeypatch, "build", PID)
    out = Path(build["data"]["out"])
    handover = (out / "infra" / "dev.md").read_text()
    assert handover.count("## aws_sqs_queue") == 2
    assert "${" not in handover  # no Terraform references in what people read
    assert not (out / "infra" / "prod.json").exists()  # prod still creates everything


class _Iam:
    def get_role(self, RoleName: str) -> dict[str, Any]:  # noqa: N803 - boto3 argument names
        if RoleName.endswith("t2-checksum"):
            raise _NoSuch("NoSuchEntity")
        return {"Role": {"Arn": f"arn:aws:iam::200000000001:role/{RoleName}"}}

    def simulate_principal_policy(self, **kw: Any) -> dict[str, Any]:
        return {"EvaluationResults": [{"EvalActionName": a, "EvalDecision": "implicitDeny" if a == "s3:PutObject"
                                       and kw["PolicySourceArn"].endswith("t1-collect") else "allowed"}
                                      for a in kw["ActionNames"]]}  # fmt: skip


class _NoSuch(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}


class _Session:
    def client(self, name: str) -> Any:
        return _Iam()


def test_infra_check_reports_missing_and_denied() -> None:
    _, contract, _ = _stack({"roles": {"mode": "existing"}})
    by = {r["address"]: r for r in check(_Session(), contract)}
    assert by["aws_iam_role.t2_checksum"]["status"] == "missing"
    assert by["aws_iam_role.t3_upload"]["status"] == "ok"
    assert by["aws_iam_role_policy.t1_collect"]["status"] == "denied"
    assert "s3:PutObject" in by["aws_iam_role_policy.t1_collect"]["detail"]
    assert by["aws_iam_role_policy.t3_upload"]["status"] == "ok"


IAC = os.environ.get("FACTORY_IAC_TOOLS")


@pytest.mark.iac
@pytest.mark.skipif(not IAC, reason="set FACTORY_IAC_TOOLS (scripts/install-iac-tools.sh)")
@pytest.mark.parametrize("infra", [{"mode": "existing"}, {"queues": {"mode": "existing"}, "roles": {"adopt": True}}])
def test_ownership_stacks_validate_offline(tmp_path: Path, infra: dict[str, Any]) -> None:
    files, _, _ = _stack(infra)
    for name, doc in files.items():
        (tmp_path / name).write_text(json.dumps(doc, indent=2, sort_keys=True))
    tools = Path(str(IAC))
    tf = str(tools / "bin" / "terraform")
    subprocess.run([tf, "init", "-backend=false", "-input=false", "-no-color", f"-plugin-dir={tools / 'plugins'}"],
                   cwd=tmp_path, check=True, capture_output=True, timeout=300)  # fmt: skip
    v = subprocess.run(
        [tf, "validate", "-json", "-no-color"], cwd=tmp_path, capture_output=True, text=True, timeout=300
    )
    assert json.loads(v.stdout)["valid"], v.stdout
