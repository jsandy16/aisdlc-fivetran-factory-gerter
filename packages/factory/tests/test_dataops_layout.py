"""Change request 2 (ADR-0026): the dataops layout — DataOps DAG JSON, readable task.py, the team repository.

Synthetic names, accounts and data only.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import boto3
import pytest
from cli_helpers import Run
from factory_spec.repo import load_repo
from factory_spec.validate import validate_dir
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[3]
UNIT_REPO = ROOT / "packages" / "factory_runtime" / "tests" / "data" / "unit_repo"
ENVS = ("dev", "sit", "ppt", "prd")
REGION = "eu-north-1"
PID = "s3_copy"
IMAGE = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/al:1"


def _init(cli: Run, root: Path, *extra: str) -> dict[str, Any]:
    rc, env = cli("--json", "init", str(root), "--layout", "dataops", "--team", "my_team", "--project", "acme-demo",
                  "--dist-bucket", "acme-demo-dist", "--base-image", IMAGE,
                  "--ca-bundle", "/etc/pki/tls/certs/ca-bundle.crt", "--proxy", "http://proxy.acme.example:9480",
                  *extra)  # fmt: skip
    assert rc == 0, env
    return dict(env)


def _platform(root: Path) -> None:
    for i, env in enumerate(ENVS):
        p = root / "platform-settings" / f"{env}.json"
        d = json.loads(p.read_text())
        d["region"] = REGION
        d["accounts"]["my_team"]["account_id"] = f"20000000000{i}"
        d["dataops"]["state_bucket"] = f"acme-demo-state-{env}"
        d["dataops"]["vault"]["address"] = "https://vault.dataops.acme.example"
        p.write_text(json.dumps(d, indent=2))


def _fill(spec: Path) -> None:
    def per_env(name: str, pairs: list[tuple[str, str]]) -> None:
        out, env = [], None
        for line in (spec / name).read_text().splitlines():
            m = re.match(r"^  (dev|sit|ppt|prd):", line)
            env = m.group(1) if m else env
            for a, b in pairs:
                if env and a in line:
                    line = line.replace(a, b.format(env=env))
            out.append(line)
        (spec / name).write_text("\n".join(out) + "\n")

    per_env("environments.yaml", [('name: "<REQUIRED>"', "name: acme-copy-events-{env}"),
                                  ('buckets: ["<REQUIRED>"]', "buckets: [acme-inbox-{env}]"),
                                  ('prefix_roots: ["<REQUIRED>"]', "prefix_roots: [incoming/]"),
                                  ('topic: "<REQUIRED>"', "topic: acme-alerts-{env}")])  # fmt: skip
    per_env("destinations.yaml", [('bucket: "<REQUIRED>"', "bucket: acme-archive-{env}"),
                                  ('kms_key: "<REQUIRED>"', "kms_key: alias/aws/s3"),
                                  ('max_classification: "<REQUIRED>"', "max_classification: internal")])  # fmt: skip
    p = spec / "pipeline.yaml"
    p.write_text(p.read_text().replace('"<REQUIRED>", data_engineer: "<REQUIRED>", governance: "<REQUIRED>", '
                                       'platform: "<REQUIRED>"', '"@alice-ba", data_engineer: "@bob-de", '
                                       'governance: "@carol-qa", platform: "@dan-platform"')
                 .replace('cost_centre: "<REQUIRED>"', "cost_centre: CC100")
                 .replace('budget_per_run_usd: "<REQUIRED>", budget_per_month_usd: "<REQUIRED>"',
                          'budget_per_run_usd: "0.01", budget_per_month_usd: "5.00"'))  # fmt: skip
    s = spec / "security.yaml"
    s.write_text(s.read_text().replace('gxp_tier: "<REQUIRED>"', "gxp_tier: non_gxp")
                 .replace('data_classification: "<REQUIRED>"', "data_classification: internal")
                 .replace('allowed_regions: ["<REQUIRED>"]', f"allowed_regions: [{REGION}]"))  # fmt: skip


@pytest.fixture
def repo(cli: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "team"
    _init(cli, root)
    _platform(root)
    monkeypatch.chdir(root)
    rc, env = cli("--json", "new", PID, "--starter", "s3_to_s3_event")
    assert rc == 0, env
    _fill(root / "pipelines" / PID / "spec")
    for cmd in ("validate", "lock", "build"):
        rc, env = cli("--json", cmd, PID)
        assert rc == 0, (cmd, env["issues"])
    return root


def test_init_writes_the_team_repository(cli: Run, tmp_path: Path) -> None:
    root = tmp_path / "team"
    env = _init(cli, root)
    for rel in ("pyproject.toml", "docker/Dockerfile", "docker/workdir/entrypoint.py", ".github/workflows/ci.yaml",
                "AGENTS.md", "docs/DEPLOYMENT.md", "src/shared/__init__.py", "platform-settings/prd.json"):  # fmt: skip
        assert (root / rel).is_file(), rel
    toml = (root / "factory.toml").read_text()
    assert 'layout = "dataops"' in toml
    assert 'environments = ["dev", "sit", "ppt", "prd"]' in toml
    assert 'extend-exclude = ["src/shared/factory"]' in (root / "pyproject.toml").read_text()
    docker = (root / "docker/Dockerfile").read_text()
    assert 'ENV HTTPS_PROXY="http://proxy.acme.example:9480"' in docker
    assert "update-ca-trust" in docker
    assert "s3://acme-demo-dist/deployments/" in (root / "docker/workdir/entrypoint.py").read_text()
    assert any("invalid" in i["msg"] for i in env["issues"])  # the templates say what is left to fill in


def test_init_in_an_existing_repository_keeps_its_files(cli: Run, tmp_path: Path) -> None:
    root = tmp_path / "existing"
    (root / "src" / "aib_to_dpl").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "theirs"\n')
    (root / "src" / "aib_to_dpl" / "task.py").write_text("print('hand-written')\n")
    env = _init(cli, root)
    assert (root / "pyproject.toml").read_text() == '[project]\nname = "theirs"\n'
    assert "pyproject.toml" in env["data"]["kept"]
    assert any(a.startswith("pyproject.toml dependencies: ") and "pydantic" in a
               for a in env["data"]["add_to_existing_files"])  # fmt: skip


def test_new_fits_the_starter_to_the_repository(repo: Path) -> None:
    doc = (repo / "pipelines" / PID / "spec" / "environments.yaml").read_text()
    assert list(re.findall(r"^  ([a-z]+):", doc, re.M)) == list(ENVS)
    assert "orchestration: dataops" in doc
    assert "orchestration: mwaa" not in doc


def test_build_writes_the_dataops_files(repo: Path) -> None:
    dag = json.loads((repo / "dags" / "s3-copy.json").read_text())
    assert list(dag) == ["name", "schedule", "version", "tasks"]
    assert dag == {"name": "s3-copy", "schedule": "*/5 * * * *", "version": "v3", "tasks": {"s3-copy": {
        "docker": {}, "envs": {"PYTHONPATH": "/workdir/src"}, "task_entrypoint": "s3_copy/task.py"}}}  # fmt: skip
    task = (repo / "src" / PID / "task.py").read_text()
    assert re.findall(r"^def (\w+)", task, re.M) == ["read_event", "list_object", "copy", "t1_copy", "run_pipeline",
                                                      "main"]  # fmt: skip
    assert '"""Take the S3 event that started this run, exactly as it was received."""' in task
    for env in ENVS:
        cfg = json.loads((repo / "src" / PID / f"config_{env}.json").read_text())
        assert cfg["runtime"]["staging_bucket"] == f"acme-demo-state-{env}"
        assert cfg["poll"]["queue_url"].endswith(f"/acme-copy-events-{env}")
        schema = json.loads((repo / "docs" / "config-schemas" / f"{PID}.schema.json").read_text())
        assert set(cfg["runtime"]) <= set(schema["properties"]["runtime"]["properties"])  # names and places only
    assert (repo / "src" / "shared" / "factory" / "factory_runtime" / "container.py").is_file()
    manifest = json.loads((repo / "pipelines" / PID / "BUILD-MANIFEST.json").read_text())
    assert manifest["layout"] == "dataops"
    assert "src/s3_copy/task.py" in manifest["files"]
    assert not (repo / "build").exists()


def test_rebuild_is_identical_and_verify_finds_hand_edits(cli: Run, repo: Path) -> None:
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert cli("--json", "build", PID)[0] == 0
    assert {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()} == before
    assert cli("--json", "verify", PID)[1]["ok"] is True
    task = repo / "src" / PID / "task.py"
    task.write_text(task.read_text().replace('task.step("r_copy")', "pass"))
    (repo / "src" / PID / "helper.py").write_text("x = 1\n")
    (repo / "src" / PID / "config_sit.json").unlink()
    codes = {(i["code"], i["loc"]) for i in cli("--json", "verify", PID)[1]["issues"]}
    assert ("generated_file_edited", "src/s3_copy/task.py") in codes
    assert ("generated_file_unexpected", "src/s3_copy/helper.py") in codes
    assert ("generated_file_missing", "src/s3_copy/config_sit.json") in codes


def test_build_never_overwrites_a_hand_written_pipeline(cli: Run, repo: Path, tmp_path: Path) -> None:
    shutil.copytree(repo / "pipelines" / PID, tmp_path / "copy")
    (repo / "src" / "aib_to_dpl").mkdir()
    (repo / "src" / "aib_to_dpl" / "task.py").write_text("print('hand-written')\n")
    shutil.copytree(tmp_path / "copy", repo / "pipelines" / "aib_to_dpl")
    (repo / "pipelines" / "aib_to_dpl" / "BUILD-MANIFEST.json").unlink()
    spec = repo / "pipelines" / "aib_to_dpl" / "spec" / "pipeline.yaml"
    spec.write_text(spec.read_text().replace("pipeline_id: s3_copy", "pipeline_id: aib_to_dpl"))
    env = repo / "pipelines" / "aib_to_dpl" / "spec" / "environments.yaml"
    env.write_text(env.read_text().replace("acme-copy-events-", "acme-aib-events-").replace("acme-inbox-", "acme-aib-"))
    assert cli("--json", "lock", "aib_to_dpl")[0] == 0
    rc, out = cli("--json", "build", "aib_to_dpl")
    assert rc != 0
    assert "src/aib_to_dpl/task.py" in out["issues"][0]["msg"]
    assert (repo / "src" / "aib_to_dpl" / "task.py").read_text() == "print('hand-written')\n"


def _task_module(repo: Path) -> Any:
    spec = importlib.util.spec_from_file_location("generated_task", repo / "src" / PID / "task.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_generated_task_copies_a_new_object(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("MOTO_ACCOUNT_ID", "200000000000")
    monkeypatch.setenv("project_env", "dev")
    monkeypatch.setenv("FACTORY_WORKDIR", str(tmp_path / "work"))
    task = _task_module(repo)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        for b in ("acme-demo-state-dev", "acme-inbox-dev", "acme-archive-dev"):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_bucket_versioning(Bucket="acme-inbox-dev", VersioningConfiguration={"Status": "Enabled"})
        put = s3.put_object(Bucket="acme-inbox-dev", Key="incoming/visit-01.csv", Body=b"id\n1\n")
        sqs = boto3.client("sqs", region_name=REGION)
        url = sqs.create_queue(QueueName="acme-copy-events-dev")["QueueUrl"]
        obj = {"key": "incoming/visit-01.csv", "size": 5, "sequencer": "0A", "versionId": put["VersionId"]}
        event = {
            "Records": [{"eventName": "ObjectCreated:Put", "s3": {"bucket": {"name": "acme-inbox-dev"}, "object": obj}}]
        }
        sqs.send_message(QueueUrl=url, MessageBody=json.dumps(event))
        assert task.main() == 0
        got = s3.get_object(Bucket="acme-archive-dev", Key="copies/dev/visit-01.csv")["Body"].read()
        assert got == b"id\n1\n"
        sqs.send_message(QueueUrl=url, MessageBody=json.dumps(event))  # the same event again: acknowledged, no rerun
        assert task.main() == 0
        assert sqs.receive_message(QueueUrl=url, WaitTimeSeconds=0).get("Messages") is None


def test_the_generated_tests_pass(repo: Path) -> None:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(repo / "tests")],
                       cwd=repo, capture_output=True, text=True, timeout=300)  # fmt: skip
    assert r.returncode == 0, r.stdout[-3000:]


def test_dataops_rejects_what_it_cannot_run(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    shutil.copytree(UNIT_REPO, root, ignore=shutil.ignore_patterns("__pycache__"))
    toml = (
        'api_version = "factory/v1"\nteam = "clinops_data"\nenvironments = ["dev"]\nplatform_settings = "ps"\n'
        'layout = "dataops"\n\n[dataops]\nproject = "acme"\ndist_bucket = "acme-dist"\nbase_image = "al:1"\n'
    )
    (root / "factory.toml").write_text(toml)
    (root / "ps").mkdir()
    from factory.gen.dataops.skeleton import platform_template

    ps = platform_template("dev", "clinops_data", "eu-west-1")
    ps["accounts"]["clinops_data"]["account_id"] = "200000000001"
    ps["placements"]["mwaa"] = {"account": "@team", "role": "orchestration"}
    ps["dataops"] = {"state_bucket": "acme-state-dev"}
    (root / "ps" / "dev.json").write_text(json.dumps(ps))
    repo = load_repo(root)
    assert repo.platform_issues == []
    issues = validate_dir(root / "pipelines" / "unit_fivetran", repo).issues
    assert any(i.code == "layout_unsupported" and "fivetran.sync" in i.msg for i in issues)
