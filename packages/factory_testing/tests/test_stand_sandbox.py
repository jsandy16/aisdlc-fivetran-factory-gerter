"""Phase 8 sandbox helpers: the preflight refuses all but the allow-listed sandbox; settings render and validate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from factory_testing.sandbox import PreflightRefusedError, main, preflight, settings

SANDBOX = "123456789012"


class _Client:
    def __init__(self, account: str, fail: bool) -> None:
        self.account, self.fail = account, fail

    def get_caller_identity(self) -> dict[str, str]:
        if self.fail:
            raise PermissionError("InvalidClientTokenId")
        return {"Account": self.account}

    def __getattr__(self, name: str) -> Any:
        return lambda **_: {}


class _Session:
    region_name = "eu-west-1"

    def __init__(self, account: str = SANDBOX, fail: bool = False) -> None:
        self.account, self.fail = account, fail
        self.calls: list[str] = []

    def client(self, name: str) -> _Client:
        self.calls.append(name)
        return _Client(self.account, self.fail)


def test_preflight_passes_only_for_the_allow_listed_account() -> None:
    report = preflight(_Session(), {"FACTORY_SANDBOX_ACCOUNT_ID": SANDBOX})
    assert report["account"] == "…9012"
    assert set(report["services"].values()) == {"reachable"}
    assert 0.5 < report["approx_usd_per_hour"] < 2


@pytest.mark.parametrize(
    ("session", "environ", "why"),
    [
        (_Session(), {}, "set FACTORY_SANDBOX_ACCOUNT_ID"),
        (_Session(account="999999999999"), {"FACTORY_SANDBOX_ACCOUNT_ID": SANDBOX}, "not the sandbox"),
        (_Session(fail=True), {"FACTORY_SANDBOX_ACCOUNT_ID": SANDBOX}, r"not usable \(PermissionError\)"),
    ],
)
def test_preflight_refuses(session: _Session, environ: dict[str, str], why: str) -> None:
    with pytest.raises(PreflightRefusedError, match=why):
        preflight(session, environ)
    assert set(session.calls) <= {"sts"}  # nothing beyond the identity check runs


def test_settings_render_from_stack_outputs(tmp_path: Path) -> None:
    outputs = {"account_id": SANDBOX, "region": "eu-west-1", "team": "clinops_data", "env": "dev",
               "staging_bucket": f"factory-clinops-data-dev-{SANDBOX}-staging",
               "bundle_store": f"factory-clinops-data-dev-{SANDBOX}-bundles",
               "audit_store": f"factory-clinops-data-dev-{SANDBOX}-audit",
               "run_state_table": f"factory-clinops-data-dev-{SANDBOX}-run-state",
               "kms_aliases": {"staging": "alias/factory-clinops-data-staging",
                               "audit": "alias/factory-clinops-data-audit", "platform": "alias/x"},
               "mwaa_environment": "factory-dev", "eks_cluster": "factory-clinops-data-dev"}  # fmt: skip
    doc = settings(outputs)
    assert {a["account_id"] for a in doc["accounts"].values()} == {SANDBOX}
    assert doc["teams"]["clinops_data"]["eks"]["namespace"] == "factory-clinops-data"
    assert "vault" not in doc
    (tmp_path / "o.json").write_text(json.dumps({"value": outputs}))
    assert main(["settings", "--outputs", str(tmp_path / "o.json"), "--out", str(tmp_path / "s")]) == 0
    assert json.loads((tmp_path / "s" / "dev.json").read_text()) == doc
