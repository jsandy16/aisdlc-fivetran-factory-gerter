"""Runtime core: run-state, staging, bundles, the run pin, the executor's error precedence, and the entry points."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import boto3
import pytest
from factory_core.errors import ConfigError, DataError, SecurityError
from factory_runtime.aws import AwsClients
from factory_runtime.bundles import load_bundle
from factory_runtime.config import ENV_VARS, from_environ
from factory_runtime.envcheck import check_environment
from factory_runtime.lambda_handler import handle
from factory_runtime.ops import load_all
from factory_runtime.staging import Staging
from factory_runtime.state import MemoryRunState
from factory_runtime.state.dynamodb import DynamoRunState
from factory_spec.artifact_models import PortalSnapshot
from factory_spec.catalog import op_catalog
from rt_helpers import (
    REGION,
    UNIT_REPO,
    LogCapture,
    copy_repo,
    make_config,
    publish_bundle,
    run_task,
    setup_stores,
)

CONF = {"request_id": "REQ-00000001", "study_id": "ONC101", "n": 5}
PIPE = "unit_naming"


@pytest.fixture
def unit(tmp_path: Path, workdir: Path, aws: None) -> dict[str, Any]:
    repo = copy_repo(UNIT_REPO, tmp_path / "repo", environments=["dev"])
    pdir = repo / "pipelines" / PIPE
    cfg = make_config(workdir, PIPE)
    setup_stores(cfg)
    uri, sha = publish_bundle(cfg, pdir)
    return {"cfg": cfg, "uri": uri, "sha": sha, "dir": pdir, "repo": repo}


def _state(cfg: Any) -> DynamoRunState:
    return DynamoRunState(boto3.client("dynamodb", region_name=REGION), cfg.run_state_table, cfg.pipeline_id)


# ---------------------------------------------------------------- run-state


@pytest.mark.parametrize("kind", ["memory", "dynamo"])
def test_run_state_semantics(kind: str, unit: dict[str, Any]) -> None:
    st: Any = MemoryRunState(PIPE) if kind == "memory" else _state(unit["cfg"])
    assert st.get("r1", "PIN") is None
    created, rec = st.put_if_absent("r1", "PIN", {"a": 1})
    assert created
    assert rec.doc == {"a": 1}
    created, rec = st.put_if_absent("r1", "PIN", {"a": 2})
    assert not created
    assert rec.doc == {"a": 1}
    assert st.transition("r1", "DISP", allowed_from=[None], to_state="received", doc={"x": 1})
    assert not st.transition("r1", "DISP", allowed_from=[None], to_state="received")
    assert st.transition("r1", "DISP", allowed_from=["received"], to_state="triggered", doc={"y": 2})
    rec = st.get("r1", "DISP")
    assert rec.state == "triggered"
    assert rec.doc == {"x": 1, "y": 2}


@pytest.mark.parametrize("kind", ["memory", "dynamo"])
def test_sequence_numbers_are_stable_per_run_under_races(kind: str, unit: dict[str, Any]) -> None:
    st: Any = MemoryRunState(PIPE) if kind == "memory" else _state(unit["cfg"])
    results: list[int] = []
    lock = threading.Lock()

    def take(run_dir: str) -> None:
        n = st.allocate_seq("names", run_dir)
        with lock:
            results.append(n)

    threads = [threading.Thread(target=take, args=("run-a",)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1  # every retry of one run gets the same number
    assert st.allocate_seq("names", "run-b") > results[0]
    assert st.allocate_seq("names", "run-a") == results[0]


# ---------------------------------------------------------------- staging


def test_staging_write_once_compare_and_adopt(unit: dict[str, Any]) -> None:
    cfg = unit["cfg"]
    stg = Staging(AwsClients(cfg), cfg.staging_bucket, None, PIPE, "run-1")
    assert stg.put_once("a.bin", b"x", content_type="application/octet-stream")
    assert not stg.put_once("a.bin", b"x")
    with pytest.raises(DataError):
        stg.put_once("a.bin", b"y")
    first = PortalSnapshot(endpoint="e", destinations={"x": "s3://b/p/"}, values={})
    second = PortalSnapshot(endpoint="e", destinations={"x": "s3://b/q/"}, values={})
    assert stg.put_doc("snap.json", first) == first
    assert stg.put_doc("snap.json", second, adopt=True) == first  # the first answer is the run's custody record
    with pytest.raises(DataError):
        stg.put_doc("snap.json", second)
    with pytest.raises(ConfigError):
        stg.get_bytes("missing.json")
    assert stg.exists("snap.json")
    assert not stg.exists("missing.json")


# ---------------------------------------------------------------- bundles


def test_bundle_loads_and_is_cached(unit: dict[str, Any]) -> None:
    cfg = unit["cfg"]
    b = load_bundle(AwsClients(cfg), cfg, unit["uri"], unit["sha"])
    assert b.spec.pipeline_id == PIPE
    assert (cfg.workdir / "bundles" / unit["sha"] / "spec" / "pipeline.yaml").is_file()
    assert load_bundle(AwsClients(cfg), cfg, unit["uri"], unit["sha"]).sha256 == unit["sha"]


def test_bundle_location_and_hash_are_enforced(unit: dict[str, Any]) -> None:
    cfg = unit["cfg"]
    aws = AwsClients(cfg)
    other = f"s3://{cfg.staging_bucket}/{cfg.bundle_prefix}{unit['sha']}.zip"
    with pytest.raises(SecurityError) as ei:
        load_bundle(aws, cfg, other, unit["sha"])
    assert ei.value.code == "bundle_location_invalid"
    # right place, wrong bytes
    fake_sha = "f" * 64
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=cfg.bundle_store, Key=f"{cfg.bundle_prefix}{fake_sha}.zip", Body=b"not the bundle"
    )
    with pytest.raises(SecurityError) as ei:
        load_bundle(aws, cfg, f"s3://{cfg.bundle_store}/{cfg.bundle_prefix}{fake_sha}.zip", fake_sha)
    assert ei.value.code == "bundle_hash_mismatch"


def test_bundle_framework_mismatch_is_config(unit: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import factory_runtime.bundles as rb

    monkeypatch.setattr(rb, "lock_text", lambda spec: "different reading of the spec\n")
    with pytest.raises(ConfigError) as ei:
        load_bundle(AwsClients(unit["cfg"]), unit["cfg"], unit["uri"], unit["sha"])
    assert ei.value.code == "bundle_framework_mismatch"


# ---------------------------------------------------------------- executor via the entry point


def test_task_succeeds_audits_and_is_idempotent(unit: dict[str, Any]) -> None:
    cfg = unit["cfg"]
    with LogCapture() as logs:
        assert run_task(cfg, unit["uri"], unit["sha"], "t1_name", "REQ-00000001", conf=CONF) == 0
        assert run_task(cfg, unit["uri"], unit["sha"], "t1_name", "REQ-00000001", attempt=2, conf=CONF) == 0
    assert [e["status"] for e in logs.events("run_exit")] == ["succeeded", "already_done"]
    st = _state(cfg)
    from factory_core.runkey import run_dir

    rdir = run_dir("REQ-00000001")
    assert st.get(rdir, "PIN") is not None
    assert st.get(rdir, "TASK#t1_name").doc["rules"] == ["r_name", "r_check_name", "r_check_n", "r_warn_big"]
    keys = [
        o["Key"] for o in boto3.client("s3", region_name=REGION).list_objects_v2(Bucket=cfg.audit_store)["Contents"]
    ]
    assert keys == [f"{PIPE}/{rdir}/t1_name/attempt-1.jsonl"]
    body = boto3.client("s3", region_name=REGION).get_object(Bucket=cfg.audit_store, Key=keys[0])["Body"].read()
    assert body.count(b'"type":"rule_result"') == 4
    # run_key is logged as a hash only
    assert "REQ-00000001" not in logs.text


@pytest.mark.parametrize(
    ("n", "on_failure", "code"),
    [
        (0, None, 65),  # r_check_n fails -> data (its on_failure)
        (0, {"action": "fail", "error_class": "config"}, 78),
        (0, {"action": "quarantine", "error_class": "data"}, 65),
        (5000, None, 0),  # r_warn_big only warns
    ],
)
def test_check_failures_follow_on_failure(
    unit: dict[str, Any], n: int, on_failure: dict[str, str] | None, code: int
) -> None:
    from factory_core import yamlio

    cfg = unit["cfg"]
    uri, sha = unit["uri"], unit["sha"]
    if on_failure is not None:
        f = unit["dir"] / "spec" / "rules.yaml"
        doc = yamlio.load_path(f)
        doc["rules"][2]["on_failure"] = on_failure
        yamlio.dump_path(doc, f)
        from factory_spec.load import load_spec
        from factory_spec.lock import write_lock

        spec = load_spec(unit["dir"]).spec
        assert spec is not None
        write_lock(spec)
        uri, sha = publish_bundle(cfg, unit["dir"])
    with LogCapture() as logs:
        assert run_task(cfg, uri, sha, "t1_name", f"REQ-{n:08d}", conf={**CONF, "n": n}) == code
    if on_failure and on_failure["action"] == "quarantine":
        from factory_core.runkey import run_dir

        assert _state(cfg).get(run_dir(f"REQ-{n:08d}"), "QUAR#t1_name#r_check_n") is not None
    if code == 0:
        assert logs.events("rule_warning")


def test_pinned_bundle_wins_and_image_mismatch_fails(unit: dict[str, Any]) -> None:
    cfg = unit["cfg"]
    assert run_task(cfg, unit["uri"], unit["sha"], "t1_name", "REQ-00000002", conf=CONF) == 0
    # a newer bundle published mid-run: a later attempt of the same run still uses the pinned one
    (unit["dir"] / "spec" / "pipeline.yaml").write_text(
        (unit["dir"] / "spec" / "pipeline.yaml").read_text().replace("Unit naming pipeline", "Unit naming v2")
    )
    from factory_spec.load import load_spec
    from factory_spec.lock import write_lock

    spec = load_spec(unit["dir"]).spec
    assert spec is not None
    write_lock(spec)
    uri2, sha2 = publish_bundle(cfg, unit["dir"])
    assert sha2 != unit["sha"]
    st = _state(cfg)
    from factory_core.runkey import run_dir

    st.client.delete_item(
        TableName=cfg.run_state_table,
        Key={"pk": {"S": f"{PIPE}#{run_dir('REQ-00000002')}"}, "sk": {"S": "TASK#t1_name"}},
    )
    with LogCapture() as logs:
        assert run_task(cfg, uri2, sha2, "t1_name", "REQ-00000002", attempt=2, conf=CONF) == 0
    assert logs.events("run_pinned_bundle")
    other_image = cfg.model_copy(update={"image_digest": "sha256:" + "1" * 64})
    assert run_task(other_image, uri2, sha2, "t1_name", "REQ-00000002", attempt=3, conf=CONF) == 78


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"env": "test"}, 78),  # no binding for test
        ({"region": "us-east-1"}, 77),  # residency: eu-west-1 only
        ({"team": "other_team"}, 78),
    ],
)
def test_environment_checks(unit: dict[str, Any], change: dict[str, str], code: int) -> None:
    cfg = unit["cfg"].model_copy(update=change)
    if "env" in change or "team" in change:
        # same stores and bundle location so only the check under test can fail
        cfg = cfg.model_copy(
            update={
                "staging_bucket": unit["cfg"].staging_bucket,
                "run_state_table": unit["cfg"].run_state_table,
                "audit_store": unit["cfg"].audit_store,
            }
        )
    uri = unit["uri"] if "team" not in change else unit["uri"].replace("clinops_data", "other_team")
    if "team" in change:
        boto3.client("s3", region_name=REGION).copy_object(
            Bucket=cfg.bundle_store,
            Key=uri.split(f"{cfg.bundle_store}/")[1],
            CopySource={"Bucket": cfg.bundle_store, "Key": unit["uri"].split(f"{cfg.bundle_store}/")[1]},
        )
    assert run_task(cfg, uri, unit["sha"], "t1_name", "REQ-00000003", conf=CONF) == code


def test_bad_conf_unknown_task_and_usage(unit: dict[str, Any]) -> None:
    cfg = unit["cfg"]
    assert run_task(cfg, unit["uri"], unit["sha"], "t1_name", "REQ-00000004", conf={"request_id": "x"}) == 78
    assert run_task(cfg, unit["uri"], unit["sha"], "t9_nope", "REQ-00000005", conf=CONF) == 78
    from factory_runtime.run import main

    assert main(["--bundle", "x"], config=cfg, environ={}) == 78


def test_unexpected_exception_is_internal_and_transient_stays_transient(
    unit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import factory_runtime.ops.naming as naming

    cfg = unit["cfg"]

    def boom(ctx: Any) -> list[Any]:
        raise ZeroDivisionError("data value 42 must not be logged")

    monkeypatch.setitem(load_all(), "naming.render", boom)
    with LogCapture() as logs:
        assert run_task(cfg, unit["uri"], unit["sha"], "t1_name", "REQ-00000006", conf=CONF) == 70
    assert "data value 42" not in logs.text

    def flaky(ctx: Any) -> list[Any]:
        raise ConnectionResetError()

    monkeypatch.setitem(load_all(), "naming.render", flaky)
    assert run_task(cfg, unit["uri"], unit["sha"], "t1_name", "REQ-00000007", conf=CONF) == 75
    assert naming.naming_render  # the real op stays importable


def test_prod_refuses_hazardous_environment() -> None:
    with pytest.raises(SecurityError):
        check_environment("prod", {"HTTPS_PROXY": "http://proxy.example:3128"})
    with pytest.raises(SecurityError):
        check_environment("ppt", {"AWS_ENDPOINT_URL_S3": "http://localhost:5000"})
    check_environment("dev", {"HTTPS_PROXY": "http://proxy.example:3128"})


def test_config_from_environment() -> None:
    env = {
        "FACTORY_ENV": "dev",
        "FACTORY_TEAM": "clinops_data",
        "FACTORY_PIPELINE_ID": PIPE,
        "FACTORY_REGION": REGION,
        "FACTORY_STAGING_BUCKET": "factory-clinops-data-dev-staging",
        "FACTORY_RUN_STATE_TABLE": "factory-clinops-data-dev-run-state",
        "FACTORY_BUNDLE_STORE": "factory-clinops-data-dev-bundles",
        "FACTORY_AUDIT_STORE": "factory-clinops-data-dev-audit",
        "FACTORY_PLACEMENT_ACCOUNTS": '{"cro_archive": "300000000001"}',
    }
    cfg = from_environ(env)
    assert cfg.placement_accounts == {"cro_archive": "300000000001"}
    assert set(env) <= ENV_VARS
    with pytest.raises(ConfigError) as ei:
        from_environ({k: v for k, v in env.items() if k != "FACTORY_TEAM"})
    assert "FACTORY_TEAM" in ei.value.message


def test_lambda_handler_returns_payload(unit: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = unit["cfg"]
    env = {
        "FACTORY_ENV": "dev",
        "FACTORY_TEAM": "clinops_data",
        "FACTORY_PIPELINE_ID": PIPE,
        "FACTORY_REGION": REGION,
        "FACTORY_STAGING_BUCKET": cfg.staging_bucket,
        "FACTORY_RUN_STATE_TABLE": cfg.run_state_table,
        "FACTORY_BUNDLE_STORE": cfg.bundle_store,
        "FACTORY_AUDIT_STORE": cfg.audit_store,
        "FACTORY_IMAGE_DIGEST": cfg.image_digest,
        "FACTORY_WORKDIR": str(cfg.workdir),
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    event = {"bundle": unit["uri"], "sha256": unit["sha"], "task": "t1_name", "run_key": "REQ-00000008", "conf": CONF}
    assert handle(event) == {"exit_code": 0, "error_class": None, "status": "ok"}
    bad = handle({**event, "sha256": "0" * 64, "run_key": "REQ-00000009"})
    assert bad["exit_code"] in (77, 78)
    assert bad["status"] == "failed"


def test_registered_ops_are_catalog_ops() -> None:
    available = {o.op for o in op_catalog().values() if o.status == "available"}
    registered = set(load_all()) - {"expression"}
    assert registered <= available
