"""fivetran.sync against a TLS stand-in of the Fivetran REST API (Q10). Fake clock: waiting costs no time.

Outcomes: success, failure (data, 65), timeout (transient, 75), refused key (config, 78); a retried task does not
start a second sync. The API key is synthetic and must never reach a log line.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from factory_core.errors import ConfigError, DataError, TransientError
from factory_runtime.config import Overrides
from factory_testing.certs import TestCA
from factory_testing.mock_fivetran import MockFivetran
from factory_testing.netmap import netmap
from rt_helpers import UNIT_REPO, LogCapture, OpHarness, copy_repo, make_config

REGION = "eu-west-1"
KEY = {"api_key": "synthetic-ft-key-1a2b", "api_secret": "synthetic-ft-secret-9z8y7x"}
SECRET_ID = "factory/platform/fivetran-api"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_791_000_000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def ft(tmp_path: Path, workdir: Path, aws: None) -> Iterator[dict[str, Any]]:
    ca = TestCA()
    repo = copy_repo(UNIT_REPO, tmp_path / "repo", environments=["dev"])
    cfg = make_config(workdir, "unit_fivetran", fivetran_api_secret=SECRET_ID)
    s3 = boto3.client("s3", region_name=REGION)
    for b in (cfg.staging_bucket, cfg.audit_store):
        s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
    sm = boto3.client("secretsmanager", region_name=REGION)
    sm.create_secret(Name=SECRET_ID, SecretString=json.dumps(KEY))
    mock = MockFivetran(ca, api_key=KEY["api_key"], api_secret=KEY["api_secret"])
    mock.add("conn_crm_01", "grp_unit_dev", "crm_db")
    mock.add("conn_other", "grp_unit_dev", "other_db")
    clock = FakeClock()
    with mock, netmap({("api.fivetran.com", 443): ("127.0.0.1", mock.port)}):

        def harness(run_key: str = "2026-10-13") -> OpHarness:
            ov = Overrides(extra_ca_pem=ca.cert_pem, clock=clock.clock, sleep=clock.sleep)
            return OpHarness(repo / "pipelines" / "unit_fivetran", cfg, overrides=ov, run_key=run_key)

        yield {"mock": mock, "harness": harness, "clock": clock, "sm": sm}


def test_sync_success_and_retry_does_not_start_another(ft: dict[str, Any]) -> None:
    mock = ft["mock"]
    with LogCapture() as logs:
        h = ft["harness"]()
        report = h.run_rule("r_sync")[0]
    assert (report.outcome, report.triggered, report.connection_id) == ("succeeded", True, "conn_crm_01")
    assert report.succeeded_at > report.previous_succeeded_at
    assert mock.syncs_started == 1
    assert ft["clock"].slept  # it polled
    access = [e for e in h.audit.events if e["type"] == "secret_access"]
    assert [(e["secret"], e["purpose"], e["outcome"]) for e in access] == [("fivetran_api", "fivetran sync", "ok")]
    # the task is retried after the sync finished: no second sync, the same answer
    again = h.run_rule("r_sync")[0]
    assert again.succeeded_at == report.succeeded_at
    assert mock.syncs_started == 1
    basic = base64.b64encode(f"{KEY['api_key']}:{KEY['api_secret']}".encode()).decode()
    for value in (KEY["api_key"], KEY["api_secret"], basic):
        assert value not in logs.text


def test_sync_failure_is_data(ft: dict[str, Any]) -> None:
    ft["mock"].outcome = "failure"
    with pytest.raises(DataError) as ei:
        ft["harness"]().run_rule("r_sync")
    assert ei.value.code == "fivetran_sync_failed"


def test_sync_timeout_is_transient(ft: dict[str, Any]) -> None:
    ft["mock"].outcome = "never"
    with pytest.raises(TransientError) as ei:
        ft["harness"]().run_rule("r_sync")
    assert ei.value.code == "fivetran_sync_timeout"
    assert sum(ft["clock"].slept) <= 2 * 3600  # never waits past the rule's timeout (2h)


def test_refused_key_is_config_and_rate_limit_is_retried(ft: dict[str, Any]) -> None:
    ft["mock"].rate_limit_once = True
    assert ft["harness"]("2026-10-12").run_rule("r_sync")[0].outcome == "succeeded"
    ft["sm"].put_secret_value(SecretId=SECRET_ID, SecretString=json.dumps({**KEY, "api_secret": "wrong-secret-0000"}))
    with pytest.raises(ConfigError) as ei:
        ft["harness"]("2026-10-11").run_rule("r_sync")
    assert ei.value.code == "fivetran_auth_refused"


def test_connection_must_exist_once_and_be_active(ft: dict[str, Any]) -> None:
    mock = ft["mock"]
    mock.connections["conn_crm_01"]["paused"] = True
    with pytest.raises(ConfigError) as ei:
        ft["harness"]().run_rule("r_sync")
    assert ei.value.code == "fivetran_connection_paused"
    mock.add("conn_crm_02", "grp_unit_dev", "crm_db")
    with pytest.raises(ConfigError) as ei:
        ft["harness"]("2026-10-12").run_rule("r_sync")
    assert ei.value.code == "fivetran_connection_ambiguous"
    mock.connections.clear()
    with pytest.raises(ConfigError) as ei:
        ft["harness"]("2026-10-11").run_rule("r_sync")
    assert ei.value.code == "fivetran_connection_unknown"
    assert mock.syncs_started == 0
