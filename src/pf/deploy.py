"""`pf deploy` — the only path into Fivetran, normally run by CI in a GitHub Environment.

1. Assemble the FULL configuration from secrets (a non-empty configuration replaces the stored one, so a
   partial one would delete keys). Each key comes from env var PF_SECRET_<KEY>; any missing key fails the deploy.
2. `fivetran deploy <build/connector> --destination <group> --connection <name> --configuration <tmp>
   --python-version <v> --yes` (--yes, not --non-interactive: the latter cancels redeploys).
3. Apply the schedule and paused flag from pipeline.yaml through the REST API.
The temporary configuration file is written with 0600 permissions outside the repository and deleted.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .bundle import Bundle
from .envelope import PfError
from .fivetran_api import FivetranApi, apply_schedule


def configuration(lock: dict, env: str, environ: dict | None = None) -> dict:
    environ = dict(os.environ if environ is None else environ)
    cfg = {"pf_environment": env}
    missing = []
    for key in lock["secrets"].get(env, []):
        v = environ.get(f"PF_SECRET_{key.upper()}", "")
        if not v:
            missing.append(f"PF_SECRET_{key.upper()}")
        cfg[key] = v
    if missing:
        raise PfError(f"deploy needs every configuration value; missing or empty: {missing}", "missing_secrets")
    return cfg


def deploy(root: Path, b: Bundle, env: str, dry_run: bool = False) -> dict:
    assert b.pipeline and b.destination
    if env not in b.pipeline.environments:
        raise PfError(f"unknown environment '{env}' (pipeline environments: {b.pipeline.environments})", "invalid")
    build = root / "pipelines" / b.name / "build"
    lock = json.loads((build / "spec.lock.json").read_text(encoding="utf-8"))
    connector_dir = build / "connector"
    group = b.destination.destination.environments[env].fivetran_group
    py = b.pipeline.runtime.python_version
    schedule = b.pipeline.schedule[env].model_dump() if env in b.pipeline.schedule else {"sync_frequency": 360, "paused": False}
    cmd = ["fivetran", "deploy", str(connector_dir), "--destination", group, "--connection", b.name,
           "--configuration", "<temporary file>", "--python-version", py, "--yes"]
    if dry_run:
        cfg_keys = ["pf_environment", *lock["secrets"].get(env, [])]
        return {"dry_run": True, "command": cmd, "configuration_keys": cfg_keys,
                "schedule": apply_schedule(None, group, b.name, schedule, dry_run=True)}  # type: ignore[arg-type]
    cfg = configuration(lock, env)
    api_key = os.environ.get("FIVETRAN_API_KEY")
    if not api_key:
        raise PfError("FIVETRAN_API_KEY is not set", "config")
    if not shutil.which("fivetran"):
        raise PfError("the fivetran CLI is not installed (pip install 'pipeline-factory[fivetran]')", "missing_dependency")
    tmpdir = tempfile.mkdtemp(prefix="pfdeploy_")
    cfg_path = Path(tmpdir) / "configuration.json"
    try:
        fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        real = cmd[:]
        real[real.index("<temporary file>")] = str(cfg_path)
        env_vars = {**os.environ, "FIVETRAN_API_KEY": api_key}
        proc = subprocess.run(real, env=env_vars, capture_output=True, text=True, timeout=1800)
        out = (proc.stdout + proc.stderr)[-4000:]
        for v in cfg.values():  # never echo a secret
            if v and len(v) > 3:
                out = out.replace(v, "***")
        if proc.returncode != 0:
            raise PfError(f"fivetran deploy failed (exit {proc.returncode}):\n{out}", "deploy_failed")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    sched = apply_schedule(FivetranApi(api_key), group, b.name, schedule)
    return {"deployed": True, "environment": env, "destination": group, "connection": b.name, "schedule": sched, "cli_output_tail": out[-1500:]}
