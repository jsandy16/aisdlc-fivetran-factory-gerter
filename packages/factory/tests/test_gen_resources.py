"""Change request M4: AWS resources by configuration (resource catalog), alert routing, notify.publish."""

from __future__ import annotations

import fnmatch
import io
import json
import os
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from cli_helpers import copy_example
from factory.cli import main
from factory.gen.names import deploy_for
from factory.gen.terraform import generate_stack
from factory_spec.load import load_spec
from factory_spec.repo import load_repo
from factory_spec.resources import Instance, ResourceCatalog, check_instances, render, resource_catalog

from factory_core import yamlio

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "team-pipelines"
PID = "study_pkg_delivery"
FACTS = {"env": "dev", "team": "clinops_data", "pipeline_id": PID, "account": "200000000001", "region": "eu-west-1",
         "partition": "aws"}  # fmt: skip
SAMPLES = [
    Instance("ops_topic", "sns_topic", {"display_name": "ops"}),
    Instance(
        "ops_mail", "sns_subscription", {"topic": "ops_topic", "protocol": "email", "endpoint": "ops@example.com"}
    ),
    Instance(
        "slow",
        "cloudwatch_metric_alarm",
        {
            "namespace": "PipelineFactory",
            "metric": "TaskFailed",
            "dimensions": {"pipeline_id": PID},
            "notify": "ops_topic",
        },
    ),
    Instance("errors", "log_metric_filter", {"log_group": "/aws/lambda/x", "pattern": "ERROR", "metric": "Errors"}),
    Instance("board", "cloudwatch_dashboard", {"body": {"widgets": []}}),
    Instance("arrivals", "eventbridge_rule", {"event_pattern": {"source": ["aws.s3"]}, "target": "ops_topic"}),
    Instance("exports", "s3_bucket", {"expire_days": 30}),
    Instance("work", "sqs_queue", {}),
    Instance("index", "dynamodb_table", {"hash_key": "pk", "attributes": [{"name": "pk", "type": "S"}]}),
]


def test_every_catalog_type_renders_deterministically() -> None:
    assert {i.type for i in SAMPLES} == set(resource_catalog().types)
    assert check_instances(SAMPLES) == []
    a, b = render(SAMPLES, FACTS), render(SAMPLES, FACTS)
    assert a.resources == b.resources
    topic = a.resources["aws_sns_topic"]["ops_topic"]
    assert topic == {"name": "factory-clinops-data-study-pkg-delivery-ops-topic", "display_name": "ops",
                     "kms_master_key_id": "${var.kms_key_arn}"}  # fmt: skip
    assert a.resources["aws_sns_topic_subscription"]["ops_mail"]["topic_arn"] == "${aws_sns_topic.ops_topic.arn}"
    assert (
        a.resources["aws_s3_bucket_versioning"]["exports_versioning"]["bucket"] == "${aws_s3_bucket.exports_bucket.id}"
    )
    assert "range_key" not in a.resources["aws_dynamodb_table"]["index"]  # unset optional parameters disappear
    assert a.outputs["exports"]["arn_literal"] == "arn:aws:s3:::factory-clinops-data-study-pkg-delivery-exports"


def test_bad_instances_are_explained() -> None:
    problems = check_instances([
        Instance("t", "sns_topic", {"colour": "blue"}),
        Instance("s", "sns_subscription", {"topic": "nowhere", "protocol": "email", "endpoint": "a@b.example"}),
        Instance("a", "cloudwatch_metric_alarm", {"namespace": "n", "metric": "m", "notify": "q"}),
        Instance("q", "sqs_queue", {}),
        Instance("x", "no_such_type", {}),
    ])  # fmt: skip
    assert any(p.startswith("t: parameter colour") for p in problems)
    assert "s: topic names 'nowhere', which is not a resource of this pipeline" in problems
    assert "a: notify must be a sns_topic, not a sqs_queue" in problems
    assert any(p.startswith("x: unknown resource type") for p in problems)


def test_a_new_service_is_a_catalog_entry_not_code() -> None:
    """A type the framework has never seen, added as data only: it validates and renders like the built-in ones."""
    cat = ResourceCatalog.model_validate({"version": 1, "types": {"ssm_parameter": {
        "summary": "A plain SSM parameter (configuration, never a secret)",
        "params": {"value": {"type": "string", "required": True}},
        "resources": {"parameter": {"type": "aws_ssm_parameter", "lookup": "name", "import_id": "<name>",
                                    "body": {"name": "/factory/<team>/<pipeline_id>/<env>/<id>", "type": "String",
                                             "value": "<params.value>"}}},
        "outputs": {"arn": "${<tf:parameter>.arn}"}}}})  # fmt: skip
    inst = [Instance("batch_size", "ssm_parameter", {"value": "500"})]
    assert check_instances(inst, cat) == []
    out = render(inst, FACTS, cat)
    assert (
        out.resources["aws_ssm_parameter"]["batch_size"]["name"]
        == "/factory/clinops_data/study_pkg_delivery/dev/batch_size"
    )
    assert out.import_ids["aws_ssm_parameter.batch_size"] == "factory-clinops-data-study-pkg-delivery-batch-size"


def test_alert_routing_from_configuration() -> None:
    repo = load_repo(EXAMPLE)
    spec = load_spec(EXAMPLE / "pipelines" / PID).spec
    assert spec is not None
    platform = repo.platform["dev"]
    files, _ = generate_stack(spec, platform, deploy_for(platform, "clinops_data", PID, "a" * 64))
    m = files["main.tf.json"]
    alarms = m["resource"]["aws_cloudwatch_metric_alarm"]
    assert set(alarms) == {"alarm_data", "alarm_config", "alarm_security", "alarm_dlq"}  # transient routes to "none"
    assert alarms["alarm_data"]["dimensions"] == {"pipeline_id": PID, "error_class": "data"}
    assert alarms["alarm_data"]["alarm_actions"] == ["${data.aws_sns_topic.alerts_clinops_data.arn}"]
    assert alarms["alarm_dlq"]["dimensions"] == {"QueueName": "clinops-package-ready-dev-dlq"}
    assert m["data"]["aws_sns_topic"]["alerts_security"] == {"name": "factory-security-alerts-dev"}
    assert "aws_sns_topic" not in m["resource"]  # sns channels name topics that exist; nothing is created


def _cli(repo: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> dict[str, Any]:
    monkeypatch.chdir(repo)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["--json", *args])
    out: dict[str, Any] = json.loads(buf.getvalue())
    out["_code"] = code
    return out


def test_a_topic_and_a_notification_by_spec_change_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = copy_example(tmp_path / "repo")
    spec_dir = repo / "pipelines" / PID / "spec"
    env = spec_dir / "environments.yaml"
    env.write_text(env.read_text() + "resources:\n  ops_topic: {type: sns_topic, params: {display_name: Deliveries}}\n")
    rules = spec_dir / "rules.yaml"
    rules.write_text(
        rules.read_text()
        + """  - rule_id: r_notify
    category: integration
    plain_english: Tell the operations topic that the package was delivered.
    implementation:
      kind: builtin
      op: notify.publish
      params: {target: ops_topic, subject: "Package {package_id} delivered"}
    outputs: [notice_id]
"""
    )
    pipe = spec_dir / "pipeline.yaml"
    pipe.write_text(pipe.read_text().replace("rules: [r_package_name, r_upload_x, r_upload_y]",
                                             "rules: [r_package_name, r_upload_x, r_upload_y, r_notify]"))  # fmt: skip
    assert _cli(repo, monkeypatch, "lock", PID)["_code"] == 0
    validated = _cli(repo, monkeypatch, "validate", PID)
    assert validated["ok"], validated["issues"]
    out = Path(_cli(repo, monkeypatch, "build", PID)["data"]["out"])
    main_tf = json.loads((out / "terraform" / "dev" / "main.tf.json").read_text())
    assert main_tf["resource"]["aws_sns_topic"]["ops_topic"]["display_name"] == "Deliveries"
    t3 = json.loads(main_tf["resource"]["aws_iam_role_policy"]["t3_upload"]["policy"])["Statement"]
    topic_arn = "arn:aws:sns:eu-west-1:200000000001:factory-clinops-data-study-pkg-delivery-ops-topic"
    assert any(s["Action"] == ["sns:Publish"] and s["Resource"] == [topic_arn] for s in t3)  # least privilege
    dag = (out / "dags" / "dev" / f"{PID}.py").read_text()
    assert '"FACTORY_RESOURCES": "{\\"ops_topic\\": \\"' + topic_arn in dag  # the pod finds the topic by id
    rules.write_text(rules.read_text().replace("target: ops_topic", "target: nowhere"))
    assert _cli(repo, monkeypatch, "lock", PID)["_code"] != 0  # an unknown target is a validation error


IAC = os.environ.get("FACTORY_IAC_TOOLS")


@pytest.mark.iac
@pytest.mark.skipif(not IAC, reason="set FACTORY_IAC_TOOLS (scripts/install-iac-tools.sh)")
def test_every_catalog_type_validates_offline(tmp_path: Path) -> None:
    rendered = render(SAMPLES, FACTS)
    doc = {"variable": {"kms_key_arn": {"type": "string"}}, "resource": rendered.resources}
    (tmp_path / "main.tf.json").write_text(json.dumps(doc, indent=2, sort_keys=True))
    (tmp_path / "providers.tf.json").write_text(json.dumps(
        {"terraform": {"required_providers": {"aws": {"source": "hashicorp/aws", "version": "6.66.0"}}},
         "provider": {"aws": {"region": "eu-west-1"}}}))  # fmt: skip
    tools = Path(str(IAC))
    tf = str(tools / "bin" / "terraform")
    subprocess.run([tf, "init", "-backend=false", "-input=false", "-no-color", f"-plugin-dir={tools / 'plugins'}"],
                   cwd=tmp_path, check=True, capture_output=True, timeout=300)  # fmt: skip
    v = subprocess.run(
        [tf, "validate", "-json", "-no-color"], cwd=tmp_path, capture_output=True, text=True, timeout=300
    )
    assert json.loads(v.stdout)["valid"], v.stdout
    out = subprocess.run([str(tools / "ckv" / "bin" / "checkov"), "-f", str(tmp_path / "main.tf.json"),
                          "--framework", "terraform_json", "--skip-download", "-o", "json"],
                         capture_output=True, text=True, timeout=600)  # fmt: skip
    report = json.loads(out.stdout)
    report = report[0] if isinstance(report, list) else report
    waivers = yamlio.to_plain(yamlio.load_path(ROOT / "platform" / "iac-waivers.yaml"))["waivers"]

    def waived(f: dict[str, Any]) -> bool:
        name = f["resource"].rsplit(".", 1)[-1]
        return any(w["check"] == f["check_id"] and fnmatch.fnmatch(name, w["resources"]) for w in waivers)

    assert [f"{f['check_id']} {f['resource']}" for f in report["results"]["failed_checks"] if not waived(f)] == []
