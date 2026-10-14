"""Phase 8 sandbox helpers: a safety preflight, real platform settings from the stack outputs, and the run order.

    uv run python -m factory_testing.sandbox preflight            # read-only; refuses any non-sandbox account
    uv run python -m factory_testing.sandbox settings --outputs outputs.json --out DIR
    uv run python -m factory_testing.sandbox steps                # prints the deploy / spike / teardown order

Nothing here creates or deletes AWS resources: applying and destroying is done by a person, with the commands `steps`
prints, after `preflight` passes. The account must equal FACTORY_SANDBOX_ACCOUNT_ID, so a credential for any other
account (production included) stops everything before the first call that could change state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ALLOW = "FACTORY_SANDBOX_ACCOUNT_ID"
TEMPLATE = Path(__file__).resolve().parents[4] / "platform" / "platform-settings" / "dev.json"
# Approximate on-demand list prices (USD per hour, eu-west-1) for the always-on parts; check the pricing pages.
HOURLY = {"mwaa mw1.small": 0.49, "eks control plane": 0.10, "2 x t3.large nodes": 0.18, "nat gateway": 0.048}
SERVICES = (("mwaa", "ListEnvironments", "list_environments"), ("eks", "ListClusters", "list_clusters"),
            ("ecr", "DescribeRepositories", "describe_repositories"))  # fmt: skip


class PreflightRefusedError(Exception):
    """The preflight refuses to go on (wrong account, missing allow-list, service not reachable)."""


def preflight(session: Any, environ: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    allowed = env.get(ALLOW, "")
    if not (len(allowed) == 12 and allowed.isdigit()):
        raise PreflightRefusedError(f"set {ALLOW} to the sandbox account id (12 digits); nothing runs without it")
    try:
        account = session.client("sts").get_caller_identity()["Account"]
    except Exception as exc:  # expired or invalid credentials: report the error type, never the credential
        raise PreflightRefusedError(f"the credentials are not usable ({type(exc).__name__})") from None
    if account != allowed:
        raise PreflightRefusedError(
            f"the credentials belong to account …{account[-4:]}, not the sandbox …{allowed[-4:]}"
        )
    services = {}
    for svc, _op, method in SERVICES:
        try:
            getattr(session.client(svc), method)()
            services[svc] = "reachable"
        except Exception as exc:
            services[svc] = f"not reachable ({type(exc).__name__})"
    if any(v != "reachable" for v in services.values()):
        raise PreflightRefusedError(f"services not reachable: {services}")
    return {"account": f"…{account[-4:]}", "region": session.region_name, "services": services,
            "approx_usd_per_hour": round(sum(HOURLY.values()), 2), "always_on": HOURLY}  # fmt: skip


def settings(outputs: dict[str, Any], template: dict[str, Any] | None = None) -> dict[str, Any]:
    """Real platform settings for the sandbox from `terraform output -json settings` (validated by the model)."""
    from factory_spec.platform import PlatformSettings

    s = json.loads(json.dumps(template if template is not None else json.loads(TEMPLATE.read_text())))
    acct, team = outputs["account_id"], outputs["team"]
    s["environment"], s["region"] = outputs["env"], outputs["region"]
    s["accounts"] = {k: {"account_id": acct} for k in s["accounts"]}  # one sandbox account plays every role
    s["mwaa"]["environment_name"] = outputs["mwaa_environment"]
    s["teams"][team]["stores"] = {k: outputs[k] for k in ("staging_bucket", "run_state_table", "bundle_store",
                                                          "audit_store")}  # fmt: skip
    s["teams"][team]["kms_aliases"] = {k: v for k, v in outputs["kms_aliases"].items() if k in ("staging", "audit")}
    s["teams"][team]["eks"] = {"cluster": outputs["eks_cluster"], "namespace": f"factory-{team.replace('_', '-')}"}
    s.pop("vault", None)  # no Vault in the sandbox: sandbox pipelines bind AWS Secrets Manager only
    PlatformSettings.model_validate(s)
    out: dict[str, Any] = s
    return out


STEPS = """\
1. preflight            uv run python -m factory_testing.sandbox preflight
2. image + plugins      scripts/build-runtime-image.sh <ecr repo url>; scripts/sandbox/package-mwaa.sh
3. platform stack       cd platform/sandbox && terraform init && terraform apply   (about 30-40 min)
4. settings             terraform output -json settings > outputs.json;
                        uv run python -m factory_testing.sandbox settings --outputs outputs.json --out <settings dir>
5. pipeline             factory build <pipeline> --image <digest>; terraform apply build/<pipeline>/terraform/dev;
                        upload the DAG to s3://<dags bucket>/dags/; kubectl create namespace + service accounts
6. run + spikes         docs/sandbox-runbook.md (S3, S5, S9, S2, S6; S10 only with a Fivetran sandbox key)
7. teardown (same day)  terraform destroy in build/<pipeline>/terraform/dev, then in platform/sandbox
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m factory_testing.sandbox", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="read-only checks; refuses any account but the allow-listed sandbox")
    st = sub.add_parser("settings", help="render platform settings from the stack outputs")
    st.add_argument("--outputs", type=Path, required=True)
    st.add_argument("--out", type=Path, required=True)
    sub.add_parser("steps", help="print the deploy, spike and teardown order")
    args = ap.parse_args(argv)
    out = sys.stdout
    if args.cmd == "steps":
        out.write(STEPS)
        return 0
    if args.cmd == "settings":
        raw = json.loads(args.outputs.read_text())
        doc = settings(raw.get("value", raw))
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"{doc['environment']}.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        out.write(f"wrote {args.out / (doc['environment'] + '.json')}\n")
        return 0
    import boto3

    try:
        report = preflight(boto3.session.Session())
    except PreflightRefusedError as exc:
        out.write(f"REFUSED: {exc}\n")
        return 77
    out.write(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
