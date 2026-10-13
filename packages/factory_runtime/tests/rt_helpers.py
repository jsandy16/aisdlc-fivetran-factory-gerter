"""Helpers for the runtime tests (a uniquely named module, importable under pytest's default import mode).

AWS is moto, in-process (`mock_aws`) or as a threaded server for end-to-end tests. All data is synthetic.
"""

from __future__ import annotations

import io
import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import boto3
from factory_runtime.config import Overrides, RuntimeConfig
from factory_runtime.run import main
from factory_runtime.state.dynamodb import create_table
from factory_spec.bundle import build_bundle

from factory_core import jsonlog

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA = Path(__file__).parent / "data"
UNIT_REPO = DATA / "unit_repo"
EXAMPLE_REPO = REPO_ROOT / "examples" / "team-pipelines"
PLATFORM_SETTINGS = REPO_ROOT / "platform" / "platform-settings"
STARTERS = REPO_ROOT / "packages" / "factory" / "src" / "factory" / "starters"
REGION = "eu-west-1"


def copy_repo(src: Path, dest: Path, *, environments: Sequence[str] | None = None, team: str = "clinops_data") -> Path:
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
    envs = list(environments) if environments else ["dev", "test", "ppt", "prod"]
    (dest / "factory.toml").write_text(
        f'api_version = "factory/v1"\nteam = "{team}"\nenvironments = {json.dumps(envs)}\n'
        f'platform_settings = "{PLATFORM_SETTINGS.as_posix()}"\n',
        encoding="utf-8",
    )
    return dest


def make_config(workdir: Path, pipeline_id: str, *, env: str = "dev", **kw: Any) -> RuntimeConfig:
    base: dict[str, Any] = {
        "env": env,
        "team": "clinops_data",
        "pipeline_id": pipeline_id,
        "region": REGION,
        "staging_bucket": f"factory-clinops-data-{env}-staging",
        "run_state_table": f"factory-clinops-data-{env}-run-state",
        "bundle_store": f"factory-clinops-data-{env}-bundles",
        "audit_store": f"factory-clinops-data-{env}-audit",
        "image_digest": "sha256:" + "0" * 64,
        "workdir": workdir,
    }
    base.update(kw)
    return RuntimeConfig.model_validate(base)


def s3(endpoint_url: str | None = None) -> Any:
    return boto3.client("s3", region_name=REGION, endpoint_url=endpoint_url)


def setup_stores(cfg: RuntimeConfig, endpoint_url: str | None = None) -> None:
    c = s3(endpoint_url)
    for b in (cfg.staging_bucket, cfg.bundle_store, cfg.audit_store):
        c.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
    create_table(boto3.client("dynamodb", region_name=REGION, endpoint_url=endpoint_url), cfg.run_state_table)


def publish_bundle(cfg: RuntimeConfig, pipeline_dir: Path, endpoint_url: str | None = None) -> tuple[str, str]:
    b = build_bundle(pipeline_dir)
    key = f"{cfg.bundle_prefix}{b.sha256}.zip"
    s3(endpoint_url).put_object(Bucket=cfg.bundle_store, Key=key, Body=b.data)
    return f"s3://{cfg.bundle_store}/{key}", b.sha256


class LogCapture:
    def __init__(self) -> None:
        self.buf = io.StringIO()

    def __enter__(self) -> LogCapture:
        jsonlog.configure(self.buf, service="test")
        return self

    def __exit__(self, *exc: object) -> None:
        jsonlog.configure(None)

    @property
    def text(self) -> str:
        return self.buf.getvalue()

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        out = [json.loads(x) for x in self.text.splitlines() if x.strip()]
        return [e for e in out if name is None or e.get("event") == name]


def run_task(
    cfg: RuntimeConfig,
    uri: str,
    sha: str,
    task: str,
    run_key: str,
    *,
    attempt: int = 1,
    conf: dict[str, Any] | None = None,
    overrides: Overrides | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    argv = ["--bundle", uri, "--sha256", sha, "--task", task, "--run-key", run_key, "--attempt", str(attempt)]
    if conf is not None:
        argv += ["--conf", json.dumps(conf)]
    return main(argv, config=cfg, overrides=overrides, environ=environ if environ is not None else {})


# ---------------------------------------------------------------- op-level harness


class OpHarness:
    """A real RuleContext for one rule of a spec: moto-backed staging and audit, memory run-state, the task's
    secrets stack. Lets op tests run without the whole task."""

    def __init__(
        self,
        pipeline_dir: Path,
        cfg: RuntimeConfig,
        *,
        trigger_body: dict[str, Any] | None = None,
        overrides: Overrides | None = None,
        run_key: str = "REQ-00000001",
    ) -> None:
        from factory_core.canonical import canonical_bytes, sha256_hex
        from factory_core.runkey import run_dir, run_key_hash
        from factory_runtime.artifacts import ArtifactStore, RunInfo, Vars
        from factory_runtime.audit import EVENT_TYPES, AuditLog
        from factory_runtime.secrets.router import build_secrets
        from factory_runtime.services import Services
        from factory_runtime.staging import Staging
        from factory_runtime.state import MemoryRunState
        from factory_spec.artifact_models import PortalSnapshot, TriggerSnapshot
        from factory_spec.derive import derive_artifacts
        from factory_spec.load import load_spec

        spec = load_spec(pipeline_dir).spec
        assert spec is not None
        self.spec = spec
        self.cfg = cfg
        self.services = Services.build(cfg, overrides, state=MemoryRunState(spec.pipeline_id))
        self.run_dir = run_dir(run_key)
        self.staging = Staging(self.services.aws, cfg.staging_bucket, None, spec.pipeline_id, self.run_dir)
        self.store = ArtifactStore(derive_artifacts(spec), self.staging)
        trig = None
        if trigger_body is not None:
            trig = TriggerSnapshot(
                source="sqs", body=trigger_body, body_sha256=sha256_hex(canonical_bytes(trigger_body))
            )
        self.trigger = trig

        def portal(endpoint: str) -> PortalSnapshot | None:
            for name in self.store.graph.artifacts:
                if self.store.has(name) and isinstance(self.store.get(name).value, PortalSnapshot):
                    return self.store.get(name).value  # type: ignore[no-any-return]
            return None

        self.vars = Vars(spec, trig, portal)
        self.audit = AuditLog(
            self.services.aws, cfg.audit_store, None, f"{spec.pipeline_id}/{self.run_dir}/", frozenset(EVENT_TYPES)
        )
        self.secrets = build_secrets(self.services, spec, cfg.env, self.audit.record)
        self.run = RunInfo(
            run_key, self.run_dir, run_key_hash(run_key), cfg.env, spec.pipeline_id, cfg.team, 1, self.run_dir,
            1_791_000_000, "2026-10-03",
        )  # fmt: skip

    def ctx(self, rule_id: str) -> Any:
        from factory_runtime.context import RuleContext

        rule = self.spec.rule(rule_id)
        task = self.spec.task_of_rule()[rule_id]
        wd = self.cfg.workdir / "runs" / self.run_dir / task
        wd.mkdir(parents=True, exist_ok=True)
        return RuleContext(
            rule, task, self.spec, self.services, self.run, self.vars, self.store, self.staging, self.audit, wd,
            self.secrets,
        )  # fmt: skip

    def run_rule(self, rule_id: str) -> list[Any]:
        from factory_runtime.executor import _run_rule, _store_outputs

        ctx = self.ctx(rule_id)
        outs = _run_rule(ctx)
        _store_outputs(ctx, outs)
        return [self.store.get(n).value for n in ctx.rule.outputs]

    def put(self, name: str, value: Any) -> None:
        self.store.put(name, value)
