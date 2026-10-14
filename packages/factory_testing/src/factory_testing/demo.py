"""Local end-to-end demo (Phase 7): the worked example from spec to delivered files, then two changes.

    uv run python -m factory_testing.demo --out DIR      (from packages/; writes DIR/DEMO-REPORT.md)

Everything runs in this process against stand-ins: moto for AWS, TLS stand-ins for the portal and Vault, an SFTP
server with the seeded dev host key. Synthetic data and synthetic secrets only. Nothing is hand-wired: the factory CLI
validates, builds and verifies; the dispatcher and the Lambda tasks are configured from the **generated Terraform**
environment variables, the pod task from the **generated DAG**'s env_vars, and the task commands come from the
generated DAG source, rendered the way Airflow would.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import boto3
from factory.local.runner import dag_tasks, render_airflow
from factory_core.runkey import run_dir

from factory_core import jsonlog

from .certs import TestCA
from .fake_vault import FakeVault
from .keys import CLIENT_KEY, DEV_SFTP_HOST, private_openssh, seeded_ed25519
from .mock_portal import MockPortal
from .netmap import netmap
from .sftp_server import SftpServer
from .synthetic import put_package, study_package

ROOT = Path(__file__).resolve().parents[4]
EXAMPLE = ROOT / "examples" / "team-pipelines"
PLATFORM = ROOT / "platform" / "platform-settings"
PID, ENV, REGION, STUDY = "study_pkg_delivery", "dev", "eu-west-1", "ONC101"
LANDING, DELIVERY = "acme-clinops-landing-dev", "acme-clinops-delivery-dev"
PORTAL, VAULT = "portal.dev.acme.example", "vault.dev.acme.example"
CLIENT = {"client_id": "synthetic-client-id", "client_secret": "synthetic-client-secret-demo-0001"}
X, Y = f"s3://{DELIVERY}/studies/{STUDY}/in/", f"sftp://{DEV_SFTP_HOST}/inbound/{STUDY}/"
GIT = ["git", "-c", "user.email=demo@example.com", "-c", "user.name=factory demo", "-c", "commit.gpgsign=false"]


@dataclass
class Step:
    title: str
    ok: bool
    facts: dict[str, Any] = field(default_factory=dict)


class _Trigger:
    """Stands in for MWAA's REST API: records which DAG a message started, and with which conf."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str, dict[str, Any]]] = []

    def start(self, dag_id: str, dag_run_id: str, conf: dict[str, Any]) -> Literal["created", "exists"]:
        if any(r == dag_run_id for _, r, _ in self.started):
            return "exists"
        self.started.append((dag_id, dag_run_id, conf))
        return "created"


@contextlib.contextmanager
def _aws() -> Iterator[None]:
    from moto import mock_aws

    keep = {k: os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION",
                                           "AWS_PROFILE", "AWS_ENDPOINT_URL", "AWS_SESSION_TOKEN")}  # fmt: skip
    for k in ("AWS_PROFILE", "AWS_ENDPOINT_URL", "AWS_SESSION_TOKEN"):
        os.environ.pop(k, None)
    os.environ |= {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": REGION}
    try:
        with mock_aws():
            yield
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Demo:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.repo = out / "repo"
        self.pdir = self.repo / "pipelines" / PID
        self.steps: list[Step] = []
        self.logs = io.StringIO()
        self.ca = TestCA()
        self.secrets: list[str] = [CLIENT["client_secret"]]

    # ------------------------------------------------------------------ helpers
    def step(self, title: str, ok: bool, **facts: Any) -> None:
        self.steps.append(Step(title, ok, facts))
        if not ok:
            raise RuntimeError(f"demo step failed: {title}: {json.dumps(facts, default=str)[:400]}")

    def cli(self, *args: str) -> dict[str, Any]:
        from factory.cli import main

        buf = io.StringIO()
        with contextlib.chdir(self.repo), contextlib.redirect_stdout(buf):
            code = main(["--json", *args])
        out: dict[str, Any] = json.loads(buf.getvalue())
        out["_code"] = code
        return out

    def git(self, *args: str) -> str:
        proc = subprocess.run([*GIT, *args], cwd=self.repo, check=True, capture_output=True, text=True)  # noqa: S603
        return proc.stdout.strip()

    def edit(self, file: str, old: str, new: str, *, count: int = 1) -> None:
        f = self.pdir / "spec" / file
        text = f.read_text(encoding="utf-8")
        if text.count(old) < count:
            raise RuntimeError(f"{file}: '{old}' not found")
        f.write_text(text.replace(old, new, count), encoding="utf-8")

    def built(self) -> tuple[dict[str, Any], Path]:
        out = self.repo / "build" / PID
        return json.loads((out / "BUILD-MANIFEST.json").read_text()), out

    def lambda_env(self, name: str) -> dict[str, str]:
        _, out = self.built()
        tf = json.loads((out / "terraform" / ENV / "main.tf.json").read_text())
        env: dict[str, str] = tf["resource"]["aws_lambda_function"][name]["environment"]["variables"]
        return env

    def config(self, env: dict[str, str]) -> Any:
        from factory_runtime.config import from_environ

        return from_environ({**env, "FACTORY_WORKDIR": str(self.out / "work")})

    # ------------------------------------------------------------------ stages
    def setup_repo(self) -> None:
        shutil.copytree(EXAMPLE, self.repo, ignore=shutil.ignore_patterns("__pycache__", "build"))
        (self.repo / "factory.toml").write_text(
            f'api_version = "factory/v1"\nteam = "clinops_data"\nenvironments = ["dev", "test", "ppt", "prod"]\n'
            f'platform_settings = "{PLATFORM.as_posix()}"\n',
            encoding="utf-8",
        )
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-qm", "study_pkg_delivery 0.1.0")
        self.step("A team repository with the worked example (git, spec 0.1.0)", True, repo=str(self.repo))

    def build_and_verify(self, label: str) -> None:
        v = self.cli("validate", PID)
        t = self.cli("test", PID)
        b = self.cli("build", PID)
        ver = self.cli("verify", PID)
        manifest, out = self.built()
        dag = out / "dags" / ENV / f"{manifest['dag_id']}.py"
        self.step(f"{label}: validate → test → build → verify", all(r["_code"] == 0 for r in (v, t, b, ver)),
                  owner_tests_passed=t["data"]["pass"], files=b["data"]["files"], dag_id=manifest["dag_id"],
                  bundle_sha256=manifest["bundle_sha256"][:12], dag=str(dag.relative_to(self.repo)),
                  verify="build matches the spec")  # fmt: skip

    def world(self, stack: contextlib.ExitStack) -> None:
        stack.enter_context(_aws())
        cfg = self.config(self.lambda_env("dispatcher"))
        s3 = boto3.client("s3", region_name=REGION)
        for b in (LANDING, DELIVERY, cfg.staging_bucket, cfg.bundle_store, cfg.audit_store):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
        from factory_runtime.state.dynamodb import create_table

        create_table(boto3.client("dynamodb", region_name=REGION), cfg.run_state_table)
        boto3.client("secretsmanager", region_name=REGION).create_secret(
            Name="factory/clinops/dev/portal-client", SecretString=json.dumps(CLIENT)
        )
        key = private_openssh(seeded_ed25519(CLIENT_KEY)).decode()
        self.secrets += [ln for ln in key.splitlines() if len(ln) > 8]
        role = cfg.vault_role or ""
        self.vault = FakeVault(self.ca, VAULT, role=role, server_id=VAULT, namespace="clinops")
        self.vault.put("clinops/dev/partner-sftp", {"private_key": key})
        self.portal = MockPortal(self.ca, PORTAL, client_id=CLIENT["client_id"], client_secret=CLIENT["client_secret"])
        self.portal.set_targets(STUDY, X, Y)
        self.sftp = SftpServer(self.out / "sftp")
        for server in (self.vault, self.portal, self.sftp):
            stack.enter_context(server)
        jsonlog.configure(self.logs, service="demo")
        stack.callback(jsonlog.configure, None)
        self.step("Stand-ins up: S3/DynamoDB/Secrets Manager (moto), portal and Vault (TLS), partner SFTP", True,
                  x=X, y=Y, secrets="synthetic; the SFTP key lives only in the fake Vault")  # fmt: skip

    def deploy_bundle(self) -> str:
        manifest, out = self.built()
        sha = manifest["bundle_sha256"]
        uri = self.lambda_env("dispatcher")["FACTORY_BUNDLE_URI"]
        bucket, key = uri[5:].split("/", 1)
        data = (out / "bundle" / f"{sha}.zip").read_bytes()
        boto3.client("s3", region_name=REGION).put_object(Bucket=bucket, Key=key, Body=data)
        return uri

    def run(self, label: str, request_id: str, package_id: str, *, files: int = 6) -> dict[str, Any]:
        """Message → dispatcher (generated config) → the generated DAG's tasks in order → a rerun (no-op)."""
        from factory_airflow.argv import runtime_argv
        from factory_runtime.config import Overrides
        from factory_runtime.run import main
        from factory_runtime.services import Services

        from factory_runtime import dispatch

        self.deploy_bundle()
        s3 = boto3.client("s3", region_name=REGION)
        prefix = f"incoming/{STUDY}/{package_id}/"
        pkg = study_package(STUDY, package_id, files=files)
        put_package(s3, LANDING, prefix, pkg)
        message = {"request_id": request_id, "study_id": STUDY, "package_id": package_id, "bucket": LANDING,
                   "prefix": prefix, "file_count": len(pkg), "total_bytes": sum(len(f.data) for f in pkg)}  # fmt: skip
        trigger = _Trigger()
        d = dispatch.build(Services.build(self.config(self.lambda_env("dispatcher"))), trigger)
        resp, _ = d.handle_batch({"Records": [{"messageId": request_id, "body": json.dumps(message)}]})
        ((dag_id, dag_run_id, conf),) = trigger.started
        manifest, out = self.built()
        tasks = dag_tasks((out / "dags" / ENV / f"{manifest['dag_id']}.py").read_text())
        hosts = {(PORTAL, 443): ("127.0.0.1", self.portal.port), (DEV_SFTP_HOST, 22): ("127.0.0.1", self.sftp.port),
                 (VAULT, 8200): ("127.0.0.1", self.vault.port)}  # fmt: skip
        values = {"run_key": str(conf["run_key"]), "run_id": dag_run_id}
        codes: dict[str, list[int]] = {}
        for attempt in (1, 2):  # the second pass is a rerun of the whole DAG run: every task is already done
            for task, payload, pod_env in tasks:
                rendered = {k: render_airflow(v, values | {"attempt": str(attempt)}) for k, v in payload.items()}
                env = pod_env if pod_env is not None else self.lambda_env(task)
                with netmap(hosts):
                    code = main(runtime_argv(rendered), config=self.config(env),
                                overrides=Overrides(extra_ca_pem=self.ca.cert_pem), environ={})  # fmt: skip
                codes.setdefault(task, []).append(code)
        name = f"{STUDY}_{package_id}.zip"
        at_y = sorted(p.name for p in self.sftp.file(f"/inbound/{STUDY}").iterdir() if p.name.startswith(name))
        listed = s3.list_objects_v2(Bucket=DELIVERY, Prefix=f"studies/{STUDY}/in/{name}").get("Contents", [])
        at_x = sorted(o["Key"].rsplit("/", 1)[1] for o in listed)
        ok = resp == {"batchItemFailures": []} and all(c == [0, 0] for c in codes.values()) and at_x == at_y
        facts = {"dispatcher_started": dag_id, "dag_run_id": dag_run_id, "exit_codes": codes,
                 "delivered_to_x_s3": at_x, "delivered_to_y_sftp": at_y}  # fmt: skip
        self.step(f"{label}: message {request_id} → {dag_id} → delivered (rerun: no-op)", ok, **facts)
        return facts

    def change(self, label: str, request: str, edits: list[tuple[str, str, str, int]], version: str) -> dict[str, Any]:
        for file, old, new, count in edits:
            self.edit(file, old, new, count=count)
        old_version = self.git("show", f"HEAD:pipelines/{PID}/spec/pipeline.yaml").split("spec_version: ", 1)[1]
        self.edit("pipeline.yaml", f"spec_version: {old_version.split()[0]}", f"spec_version: {version}")
        lock = self.cli("lock", PID)
        plan = self.cli("change", "plan", PID, "--base", "HEAD", "--record", request)
        return {"lock": lock, "plan": plan}

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def evidence(self) -> None:
        from factory_runtime.config import from_environ

        cfg = from_environ(self.lambda_env("t1_collect") | {"FACTORY_WORKDIR": str(self.out / "work")})
        s3 = boto3.client("s3", region_name=REGION)
        audit = [o["Key"] for o in s3.list_objects_v2(Bucket=cfg.audit_store).get("Contents", [])]
        events = [json.loads(x) for x in self.logs.getvalue().splitlines() if x.strip()]
        exits = [e.get("status") for e in events if e.get("event") == "run_exit"]
        leaked = [s[:6] + "…" for s in self.secrets if s in self.logs.getvalue()]
        _, out = self.built()
        self.step("Evidence: audit trail written, run outcomes logged, no secret value in any log line", not leaked,
                  audit_objects=len(audit), run_exits={s: exits.count(s) for s in sorted(set(map(str, exits)))},
                  secret_values_in_logs=leaked or "none",
                  docs=[str(p.relative_to(self.repo)) for p in sorted((out / "docs").iterdir())],
                  changelog=sorted(p.name for p in (self.pdir / "changes").glob("*.json")))  # fmt: skip

    # ------------------------------------------------------------------ the story
    def play(self) -> list[Step]:
        self.setup_repo()
        self.build_and_verify("Spec 0.1.0")
        with contextlib.ExitStack() as stack:
            self.world(stack)
            self.run("Run 1 (spec 0.1.0)", "REQ-00000001", "PKG0042")

            c1 = self.change("Change 1", "Partners only need SHA-256 checksum files, not MD5.",
                             [("rules.yaml", "sidecars: [md5, sha256]", "sidecars: [sha256]", 2),
                              ("security.yaml", "delivery_sidecars: [md5, sha256]", "delivery_sidecars: [sha256]", 1)],
                             "0.2.0")  # fmt: skip
            p1 = c1["plan"]["data"]
            self.step("Change 1 (non-breaking): change plan, recorded as changes/0001.json", c1["plan"]["_code"] == 0,
                      request="Partners only need SHA-256 checksum files, not MD5.",
                      spec_changes=[c["path"] for c in p1["spec_changes"]], breaking=p1["breaking"],
                      dag=p1["dag_strategy"]["strategy"], files_changed=len(p1["files"]["changed"]),
                      tests_to_run=p1["tests_to_run"], approvals=p1["requires_approval"] or "none",
                      cost_delta_month=p1["cost"]["monthly_usd"]["delta"])  # fmt: skip
            self.build_and_verify("Spec 0.2.0")
            self.commit("Change 1: SHA-256 sidecars only (0.2.0)")
            r2 = self.run("Run 2 (spec 0.2.0)", "REQ-00000002", "PKG0043")
            self.step(
                "Change 1 visible in the delivery: no .md5 sidecar any more",
                not any(n.endswith(".md5") for n in r2["delivered_to_y_sftp"]),
                delivered=r2["delivered_to_y_sftp"],
            )

            run_key = ('run_key: "{request_id}"', 'run_key: "{study_id}-{request_id}"', 1)
            c2 = self.change("Change 2", "One request id may now cover several studies; key runs by study too.",
                             [("pipeline.yaml", *run_key)], "0.3.0")  # fmt: skip
            refused = {i["code"] for i in c2["plan"]["issues"]}
            self.step("Change 2 (breaking) at 0.3.0 is refused, nothing recorded", c2["plan"]["_code"] != 0 and
                      "breaking_change_needs_major" in refused, issues=sorted(refused))  # fmt: skip
            self.edit("pipeline.yaml", "spec_version: 0.3.0", "spec_version: 2.0.0")
            self.cli("lock", PID)
            p2 = self.cli("change", "plan", PID, "--base", "HEAD", "--record",
                          "One request id may now cover several studies; key runs by study too.")  # fmt: skip
            d2 = p2["data"]
            self.step("Change 2 at 2.0.0: new DAG, old one retired after draining", p2["_code"] == 0 and
                      d2["dag_strategy"]["strategy"] == "new_dag", dag_strategy=d2["dag_strategy"],
                      approvals=d2["requires_approval"], files_added=d2["files"]["added"][:4])  # fmt: skip
            self.build_and_verify("Spec 2.0.0")
            self.commit("Change 2: run key includes the study (2.0.0, new DAG)")
            r3 = self.run("Run 3 (spec 2.0.0)", "REQ-00000003", "PKG0044")
            self.step("The dispatcher now starts the new DAG; the run key carries the study", r3[
                "dispatcher_started"] == f"{PID}__v2" and r3["dag_run_id"] == run_dir(f"{STUDY}-REQ-00000003"),
                dispatcher_started=r3["dispatcher_started"], dag_run_id=r3["dag_run_id"])  # fmt: skip
            self.evidence()
        return self.steps

    def report(self) -> Path:
        lines = [
            "# Pipeline Factory: local end-to-end demo",
            "",
            "Generated by `python -m factory_testing.demo`. Stand-ins only; synthetic data and secrets.",
            "",
        ]
        for i, s in enumerate(self.steps, 1):
            lines += [f"## {i}. {s.title} — {'OK' if s.ok else 'FAILED'}", ""]
            lines += [f"- **{k}**: `{json.dumps(v, default=str)}`" for k, v in s.facts.items()]
            lines.append("")
        out = self.out / "DEMO-REPORT.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        return out


def run_demo(out: Path, echo: Callable[[str], None] | None = None) -> tuple[list[Step], Path]:
    out.mkdir(parents=True, exist_ok=True)
    demo = Demo(out)
    try:
        steps = demo.play()
    finally:
        report = demo.report()
    for i, s in enumerate(steps, 1):
        if echo:
            echo(f"{i:2}. {'OK ' if s.ok else 'ERR'} {s.title}")
    return steps, report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m factory_testing.demo", description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True, help="An empty folder for the scratch repo and the report")
    args = ap.parse_args(argv)
    if args.out.exists() and any(args.out.iterdir()):
        ap.error(f"{args.out} is not empty")
    _, report = run_demo(args.out, print)
    print(f"report: {report}")  # noqa: T201 - a command-line entry point
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
