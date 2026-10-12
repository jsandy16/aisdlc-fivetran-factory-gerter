"""`pf deploy` — the only path into Fivetran, normally run by CI in a GitHub Environment.

1. Assemble the FULL configuration from secrets (a non-empty configuration replaces the stored one, so a
   partial one would delete keys). Each key comes from env var PF_SECRET_<KEY>; any missing key fails the deploy.
2. `fivetran deploy <build/connector> --destination <group> --connection <name> --configuration <tmp>
   --python-version <v> --yes` from a temporary copy of the connector. --yes confirms the redeploy and
   configuration-replace prompts (--non-interactive would cancel redeploys; --force is deprecated). --yes would also
   accept any requirements.txt change the SDK's pipreqs check proposes, so the generated code keeps that check
   quiet (see pf_runtime/cel.py) and the deploy fails if the SDK changed requirements.txt anyway.
   The API key is passed as FIVETRAN_API_KEY, never on the command line.
3. Apply the schedule and paused flag from pipeline.yaml through the REST API.
The temporary configuration file is written with 0600 permissions outside the repository and deleted.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
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


def _mask(text: str, cfg: dict) -> str:
    """Remove every secret value, and every line of multi-line secrets (keys, certificates), from CLI output."""
    for v in sorted((v for v in cfg.values() if isinstance(v, str)), key=len, reverse=True):
        if len(v) > 3:
            text = text.replace(v, "***")
        for line in v.splitlines():
            if len(line.strip()) > 8 and "-----" not in line:
                text = text.replace(line.strip(), "***")
    return text


def _fivetran_cli() -> str | None:
    """The Connector SDK CLI from PATH, or next to this interpreter (a venv not on PATH)."""
    exe = shutil.which("fivetran")
    if exe:
        return exe
    cand = Path(sys.executable).parent / "fivetran"
    return str(cand) if cand.exists() else None


def preflight_dependencies(connector: Path) -> list[str]:
    """Before `fivetran deploy --yes`: the SDK compares requirements.txt with the versions installed HERE (pipreqs
    reads the local site-packages) and --yes accepts every proposed change. Any difference is refused now, before
    anything is uploaded. CI installs with `-c requirements.txt` so the runner matches the pins."""
    from importlib import metadata

    from .generate.connector import IMPORT_TO_DIST, _pipreqs_view

    files = {p.relative_to(connector).as_posix(): p.read_bytes() for p in connector.rglob("*.py")}
    problems = _pipreqs_view(files)
    pins = {}
    for line in (connector / "requirements.txt").read_text().split():
        name, _, ver = line.partition("==")
        pins[name.lower().replace("-", "_")] = ver
    seen_imports = set()
    try:
        from pipreqs import pipreqs as pr

        import tempfile as _t
        with _t.TemporaryDirectory() as d:
            for rel, data in files.items():
                f = Path(d) / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(data)
            seen_imports = set(pr.get_all_imports(d, encoding="utf-8"))
    except ImportError:
        pass
    for imp in sorted(seen_imports):
        dist = IMPORT_TO_DIST.get(imp)
        if not dist:
            continue
        try:
            installed = metadata.version(dist)
        except metadata.PackageNotFoundError:
            problems.append(f"{dist} is not installed here; the SDK would look '{imp}' up on PyPI")
            continue
        want = pins.get(dist.lower().replace("-", "_"))
        if want and installed != want:
            problems.append(f"{dist} {installed} is installed but {want} is pinned: the SDK would rewrite requirements.txt "
                            f"(install with: pip install ... -c {connector / 'requirements.txt'})")
    return problems


def deploy(root: Path, b: Bundle, env: str, dry_run: bool = False, acknowledge_resync: bool = False) -> dict:
    assert b.pipeline and b.destination
    if env not in b.pipeline.environments:
        raise PfError(f"unknown environment '{env}' (pipeline environments: {b.pipeline.environments})", "invalid")
    build = root / "pipelines" / b.name / "build"
    lock = json.loads((build / "spec.lock.json").read_text(encoding="utf-8"))
    connector_dir = build / "connector"
    denv = b.destination.destination.environments[env]
    group = denv.fivetran_group
    py = b.pipeline.runtime.python_version
    schedule = b.pipeline.schedule[env].model_dump() if env in b.pipeline.schedule else {"sync_frequency": 360, "paused": False}
    # --naming is fixed: the naming strategy cannot change after creation, and a FIVETRAN_NAMING variable in the CI
    # environment would otherwise decide it silently
    cmd = ["fivetran", "deploy", str(connector_dir), "--destination", group, "--connection", b.name,
           "--configuration", "<temporary file>", "--python-version", py, "--naming", "fivetran", "--yes"]
    net = denv.networking
    if net and net.proxy_agent_id:
        cmd += ["--proxy-id", net.proxy_agent_id] + (["--proxy-host-config-key", net.proxy_host_config_key] if net.proxy_host_config_key else [])
    if net and net.hybrid_deployment_agent_id:
        cmd += ["--hybrid-deployment-agent-id", net.hybrid_deployment_agent_id]
    recf = b.path / "releases" / f"{lock['version']}.json"
    record = json.loads(recf.read_text(encoding="utf-8")) if recf.exists() else None
    resync = bool(record and record.get("release", {}).get("requires_resync"))
    if dry_run:
        cfg_keys = ["pf_environment", *lock["secrets"].get(env, [])]
        return {"dry_run": True, "command": cmd, "configuration_keys": cfg_keys, "requires_resync": resync,
                "dependency_preflight": preflight_dependencies(connector_dir),
                "schedule": apply_schedule(None, group, b.name, schedule, dry_run=True)}  # type: ignore[arg-type]
    if record is None:
        raise PfError(f"no release record for {lock['version']}: run `pf release {b.name}` and commit it first", "not_released")
    if resync and not acknowledge_resync:
        raise PfError(f"version {lock['version']} changes a primary key or load mode: Fivetran needs the destination table "
                      "dropped and a full historical re-sync. Do that deliberately, then deploy with --acknowledge-resync "
                      "(and set configuration pf_full_reload=true for the re-sync).", "requires_resync")
    cfg = configuration(lock, env)
    api_key = os.environ.get("FIVETRAN_API_KEY")
    if not api_key:
        raise PfError("FIVETRAN_API_KEY is not set", "config")
    cli = _fivetran_cli()
    if not cli:
        raise PfError("the fivetran CLI is not installed (pip install 'pipeline-factory[fivetran]')", "missing_dependency")
    tmpdir = tempfile.mkdtemp(prefix="pfdeploy_")
    cfg_path = Path(tmpdir) / "configuration.json"
    work = Path(tmpdir) / "connector"
    try:
        # deploy exactly what was released: a copy whose deterministic zip matches the committed release record
        from .release import _zip

        released = record["release"]["connector_zip_sha256"]
        shutil.copytree(connector_dir, work, ignore=shutil.ignore_patterns("__pycache__", ".*", "configuration.json"))
        shutil.rmtree(work / "files", ignore_errors=True)
        if hashlib.sha256(_zip(work)).hexdigest() != released:
            raise PfError("the build differs from the released artefact for this version; rebuild, or bump the version and release", "not_released")
        drift = preflight_dependencies(work)
        if drift:
            raise PfError("dependency preflight failed (nothing was deployed):\n" + "\n".join(drift), "dependency_drift")
        req_before = (work / "requirements.txt").read_bytes()
        fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        real = [cli] + cmd[1:]
        real[real.index("<temporary file>")] = str(cfg_path)
        real[real.index(str(connector_dir))] = str(work)
        # only the API key reaches the CLI from the CI environment: FIVETRAN_NAMING, FIVETRAN_CONFIGURATION,
        # FIVETRAN_HD_AGENT_ID, FIVETRAN_PYTHON_VERSION and the like would silently change what is deployed
        env_vars = {k: v for k, v in os.environ.items() if not k.startswith("FIVETRAN_")}
        env_vars["FIVETRAN_API_KEY"] = api_key
        env_vars["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env_vars.get('PATH', '')}"  # pipreqs next to the CLI
        proc = subprocess.run(real, env=env_vars, capture_output=True, text=True, timeout=1800)
        out = _mask(proc.stdout + proc.stderr, cfg)[-4000:]  # mask the whole output before trimming it
        if proc.returncode != 0:
            raise PfError(f"fivetran deploy failed (exit {proc.returncode}):\n{out}", "deploy_failed")
        if (work / "requirements.txt").read_bytes() != req_before:
            raise PfError("the Connector SDK changed requirements.txt during deploy (dependency check); the generated pins "
                          "were not deployed as reviewed — investigate before the next sync:\n" + out[-1500:], "deploy_tampered")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    sched = apply_schedule(FivetranApi(api_key), group, b.name, schedule)
    return {"deployed": True, "environment": env, "destination": group, "connection": b.name, "schedule": sched,
            "cli_output_tail": out[-1500:]}
