"""Terraform generator: least privilege from the op contracts, deterministic output, offline validation (opt-in)."""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from factory.gen.names import deploy_for
from factory.gen.terraform import egress, generate_terraform
from factory_spec.load import load_spec
from factory_spec.repo import load_repo

from factory_core import yamlio

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_REPO = ROOT / "examples" / "team-pipelines"
UNIT_REPO = ROOT / "packages" / "factory_runtime" / "tests" / "data" / "unit_repo"


def _gen(repo_root: Path, pid: str, env: str) -> dict[str, Any]:
    repo = load_repo(repo_root)
    spec = load_spec(repo_root / "pipelines" / pid).spec
    assert spec is not None
    platform = repo.platform[env]
    return generate_terraform(spec, platform, deploy_for(platform, spec.pipeline.metadata.team, pid, "a" * 64))


def _policies(main: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {k: json.loads(v["policy"])["Statement"] for k, v in main["resource"]["aws_iam_role_policy"].items()}


def test_least_privilege_for_the_worked_example() -> None:
    files = _gen(EXAMPLE_REPO, "study_pkg_delivery", "dev")
    assert files == _gen(EXAMPLE_REPO, "study_pkg_delivery", "dev")  # deterministic
    pols = _policies(files["main.tf.json"])
    assert sorted(pols) == ["dispatcher", "t1_collect", "t2_checksum", "t3_upload"]
    for role, stmts in pols.items():
        for s in stmts:
            assert all("*" not in a for a in s["Action"]), (role, s)  # never a wildcard action
            if "*" in s["Resource"]:  # only KMS by alias/service condition, or VPC ENIs (which IAM cannot scope)
                assert "Condition" in s or all(a.startswith("ec2:") for a in s["Action"]), (role, s)
    t3 = {r for s in pols["t3_upload"] for r in s["Resource"]}
    assert "arn:aws:s3:::acme-clinops-delivery-dev/studies/*" in t3  # x/y S3 entry, its fixed root only
    assert not any("secretsmanager" in r for r in t3)  # the SFTP key is in Vault: no IAM permission needed
    t1 = {r for s in pols["t1_collect"] for r in s["Resource"]}
    assert "arn:aws:secretsmanager:eu-west-1:200000000001:secret:factory/clinops/dev/portal-client-??????" in t1
    assert "arn:aws:s3:::acme-clinops-landing-dev/incoming/*" in t1
    lister = next(s for s in pols["t1_collect"] if s["Action"] == ["s3:ListBucket"])
    assert lister["Condition"] == {"StringLike": {"s3:prefix": ["incoming/*"]}}
    rows = {"dynamodb:LeadingKeys": ["study_pkg_delivery#*"]}
    assert any(s.get("Condition", {}).get("ForAllValues:StringLike") == rows for s in pols["t1_collect"])
    res = files["main.tf.json"]["resource"]
    assert res["aws_lambda_event_source_mapping"]["trigger"]["function_response_types"] == ["ReportBatchItemFailures"]
    assert "t3_upload" in res["aws_eks_pod_identity_association"]
    assert "t3_upload" not in res["aws_lambda_function"]
    env = res["aws_lambda_function"]["t1_collect"]["environment"]["variables"]
    assert env["FACTORY_VAULT_ROLE"] == "factory-clinops_data-study_pkg_delivery"
    assert not any(k.endswith(("_TOKEN", "_PASSWORD")) for k in env)  # configuration, never secrets
    assert egress(load_spec(EXAMPLE_REPO / "pipelines" / "study_pkg_delivery").spec, "dev") == {  # type: ignore[arg-type]
        "t1_collect": ["portal.dev.acme.example:443"],
        "t2_checksum": [],
        "t3_upload": ["sftp-stub.dev.acme.example:22"],
    }


def test_cross_account_placement_and_s3_event_queue_policy() -> None:
    ppt = _policies(_gen(EXAMPLE_REPO, "study_pkg_delivery", "ppt")["main.tf.json"])
    kms = next(s for s in ppt["t3_upload"] if "kms:ResourceAliases" in json.dumps(s) and "cro-archive" in json.dumps(s))
    assert any(":kms:eu-west-1:" in a and "alias/cro-archive-inbound" in a
               for a in kms["Condition"]["ForAnyValue:StringEquals"]["kms:ResourceAliases"])  # fmt: skip
    files = _gen(UNIT_REPO, "unit_s3_event", "dev")
    pol = json.loads(files["main.tf.json"]["resource"]["aws_sqs_queue_policy"]["trigger"]["policy"])
    assert pol["Statement"][0]["Condition"]["ArnLike"]["aws:SourceArn"] == ["arn:aws:s3:::unit-source-dev"]


IAC = os.environ.get("FACTORY_IAC_TOOLS")


@pytest.mark.iac
@pytest.mark.skipif(not IAC, reason="set FACTORY_IAC_TOOLS (scripts/install-iac-tools.sh)")
@pytest.mark.parametrize(("repo", "pid", "env"), [(EXAMPLE_REPO, "study_pkg_delivery", "dev"),
                                                  (EXAMPLE_REPO, "study_pkg_delivery", "ppt"),
                                                  (UNIT_REPO, "unit_s3_event", "dev"),
                                                  (UNIT_REPO, "unit_fivetran", "dev")])  # fmt: skip
def test_offline_terraform_validate_and_checkov(tmp_path: Path, repo: Path, pid: str, env: str) -> None:
    tools = Path(str(IAC))
    for name, doc in _gen(repo, pid, env).items():
        (tmp_path / name).write_text(json.dumps(doc, indent=2, sort_keys=True))
    tf = str(tools / "bin" / "terraform")
    subprocess.run([tf, "init", "-backend=false", "-input=false", "-no-color", f"-plugin-dir={tools / 'plugins'}"],
                   cwd=tmp_path, check=True, capture_output=True, timeout=300)  # fmt: skip
    v = subprocess.run(
        [tf, "validate", "-json", "-no-color"], cwd=tmp_path, capture_output=True, text=True, timeout=300
    )
    assert json.loads(v.stdout)["valid"], v.stdout
    shutil.rmtree(tmp_path / ".terraform")
    out = subprocess.run([str(tools / "ckv" / "bin" / "checkov"), "-f", str(tmp_path / "main.tf.json"),
                          "--framework", "terraform_json", "--skip-download", "-o", "json"],
                         capture_output=True, text=True, timeout=600)  # fmt: skip
    report = json.loads(out.stdout)
    report = report[0] if isinstance(report, list) else report
    failed = report["results"]["failed_checks"] if "results" in report else []
    waivers = yamlio.to_plain(yamlio.load_path(ROOT / "platform" / "iac-waivers.yaml"))["waivers"]
    assert all(w["reason"].strip() and w["owner"] for w in waivers)

    def waived(f: dict[str, Any]) -> bool:
        name = f["resource"].rsplit(".", 1)[-1]
        return any(w["check"] == f["check_id"] and fnmatch.fnmatch(name, w["resources"])
                   and not any(fnmatch.fnmatch(name, e) for e in w.get("except", [])) for w in waivers)  # fmt: skip

    open_findings = [f"{f['check_id']} {f['resource']}" for f in failed if not waived(f)]
    assert open_findings == []
