"""`factory build` in the dataops layout (ADR-0026): the pipeline's files in the team repository itself.

    dags/<dag-name>.json                         the DataOps DAG definition (one container task)
    src/<pipeline>/task.py                       the Airflow task: readable steps calling the framework library
    src/<pipeline>/config_<env>.json             runtime bindings per environment (no secrets)
    src/<pipeline>/pipeline_bundle.zip           the locked spec the task runs (sha256 pinned in task.py)
    src/shared/factory/...                       the framework library, vendored (synced with src/, no image rebuild)
    tests/<pipeline>/test_task.py                structure and smoke tests for the team's CI
    docs/pipelines/<pipeline>.md                 trigger, steps, config keys, how to run and test
    docs/config-schemas/<pipeline>.schema.json   JSON schema of config_<env>.json

The record of what was generated is pipelines/<pipeline>/BUILD-MANIFEST.json (committed). `factory build` never
writes over a file it did not generate (a hand-written pipeline with the same name stays untouched), and `factory
verify` reports edited, missing, stale and unexpected files like the mwaa layout does.
"""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from pathlib import Path
from typing import Any

from factory_core.canonical import canonical_bytes
from factory_core.envelope import CommandError
from factory_spec.bundle import build_bundle
from factory_spec.lock import build_lock
from factory_spec.repo import DataopsRepo, Repo
from factory_spec.spec import PipelineSpec

from ..build import Rendered
from .docs import config_schema, pipeline_doc
from .task_py import generate_task_py
from .tests_py import generate_test_py

MANIFEST = "BUILD-MANIFEST.json"
DEFAULT_POLL = "*/5 * * * *"
VENDOR_ROOT = "src/shared/factory"
VENDORED = ("factory_core", "factory_spec", "factory_runtime")
_SKIP = ("__pycache__", ".pyc", "py.typed")


def _json(doc: Any, *, sort_keys: bool = True) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=sort_keys) + "\n").encode()


def dag_name(spec: PipelineSpec) -> str:
    """kebab-case like the team's DAGs; a breaking change (major ≥ 2) gets a new DAG, as in the mwaa layout."""
    major = int(spec.pipeline.metadata.spec_version.split(".", 1)[0])
    base = spec.pipeline_id.replace("_", "-")
    return base if major <= 1 else f"{base}-v{major}"


def enabled_envs(repo: Repo, spec: PipelineSpec) -> list[str]:
    envs = spec.environments.environments
    return [e for e in repo.environments if e in envs and envs[e].enabled and e in repo.platform]


def schedule(repo: Repo, spec: PipelineSpec) -> str:
    """One DAG file serves every environment, so per-environment schedules must agree."""
    trig: Any = spec.pipeline.trigger
    if trig.type == "manual":
        return "none"
    overrides = {spec.environments.environments[e].schedule_cron for e in enabled_envs(repo, spec)} - {None}
    if len(overrides) > 1:
        raise CommandError("the dataops layout has one DAG file for all environments: schedule_cron must be the same "
                           "in every environment", "invalid")  # fmt: skip
    if overrides:
        return str(next(iter(overrides)))
    return str(trig.cron) if trig.type == "schedule" else DEFAULT_POLL


def dag_json(repo: Repo, spec: PipelineSpec) -> dict[str, Any]:
    retry = spec.pipeline.reliability.retry
    doc: dict[str, Any] = {
        "name": dag_name(spec),
        "schedule": schedule(repo, spec),
        "version": _settings(repo).dag_version,
        "tasks": {
            spec.pipeline_id.replace("_", "-"): {
                "docker": {},
                "envs": {"PYTHONPATH": "/workdir/src"},
                "task_entrypoint": f"{spec.pipeline_id}/task.py",
            }
        },
    }
    if spec.pipeline.trigger.type in ("schedule", "manual") and retry.max_attempts > 1:
        doc["dag_retry_count"] = retry.max_attempts - 1
        doc["dag_retry_delay"] = f"timedelta(seconds={retry.base})"
    return doc


def _settings(repo: Repo) -> DataopsRepo:
    if repo.config.dataops is None:
        raise CommandError('factory.toml needs layout = "dataops" and a [dataops] table', "config")
    return repo.config.dataops


def allowed_env(ds: DataopsRepo) -> dict[str, str]:
    """The proxy and CA variables the image sets (docker/Dockerfile): accepted in strict environments, exactly."""
    out: dict[str, str] = {}
    if ds.proxy:
        out |= dict.fromkeys(("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"), ds.proxy)
    if ds.ca_bundle:
        out |= {"REQUESTS_CA_BUNDLE": ds.ca_bundle, "SSL_CERT_FILE": ds.ca_bundle}
    return out


def runtime_config(repo: Repo, spec: PipelineSpec, env: str) -> dict[str, Any]:
    ps = repo.platform[env]
    if ps.dataops is None:
        raise CommandError(f"the {env} platform settings have no dataops section", "config")
    ds, dps = _settings(repo), ps.dataops
    team = spec.pipeline.metadata.team
    if team not in ps.teams:
        raise CommandError(f"team '{team}' is not in the {env} platform settings", "config")
    team_account = ps.accounts[ps.teams[team].account].account_id
    placement = {a: (team_account if p.account == "@team" else ps.accounts[p.account].account_id)
                 for a, p in sorted(ps.placements.items())}  # fmt: skip
    runtime: dict[str, Any] = {
        "team": team,
        "pipeline_id": spec.pipeline_id,
        "region": ps.region,
        "staging_bucket": dps.state_bucket,
        "audit_store": dps.state_bucket,
        "bundle_store": dps.state_bucket,
        "state_store": "s3",
        "store_prefix": dps.state_prefix,
        "dag_id": dag_name(spec),
        "image_digest": "dataops",
        "placement_accounts": placement,
        "allowed_env": allowed_env(ds),
    }
    if ds.ca_bundle:
        runtime["trusted_ca_file"] = ds.ca_bundle
    if dps.vault is not None:
        v = dps.vault
        runtime |= {"dataops_vault_address": v.address, "dataops_vault_service_env": v.service_env,
                    "dataops_vault_token_secret": v.token_secret, "dataops_vault_token_region": v.token_region,
                    "dataops_vault_kv_path": v.kv_path}  # fmt: skip
    if dps.project_entity:
        runtime["project_entity"] = dps.project_entity
    doc: dict[str, Any] = {"environment": env, "runtime": runtime}
    trig: Any = spec.pipeline.trigger
    if trig.type in ("sqs", "s3_event"):
        queue = spec.environments.environments[env].queues[trig.queue].name
        url = f"https://sqs.{ps.region}.amazonaws.com/{team_account}/{queue}"
        doc["poll"] = {"queue_url": url, "max_messages": 10, "wait_seconds": 1, "max_runs": 100}
    return doc


def vendored_files() -> dict[str, bytes]:
    """The framework library as installed (pure Python and its catalogs), under src/shared/factory/."""
    out: dict[str, bytes] = {}
    for pkg in VENDORED:
        root = Path(str(resources.files(pkg)))
        for f in sorted(root.rglob("*")):
            rel = f.relative_to(root).as_posix()
            if f.is_file() and not any(s in rel for s in _SKIP):
                out[f"{VENDOR_ROOT}/{pkg}/{rel}"] = f.read_bytes()
    out[f"{VENDOR_ROOT}/README.md"] = (
        b"# Pipeline Factory library (vendored)\n\nGenerated by `factory build`; do not edit. The generated "
        b"`src/<pipeline>/task.py` files import it from here, so a framework upgrade is a rebuild, not an image "
        b"rebuild. Linting, type checks, coverage and Sonar skip this folder (see pyproject.toml).\n"
    )
    return out


def render_dataops(repo: Repo, pipeline_dir: Path) -> Rendered:
    bundle = build_bundle(pipeline_dir)  # validates; refuses a stale lock
    from factory_spec.load import load_spec

    spec = load_spec(pipeline_dir).spec
    if spec is None:  # pragma: no cover - build_bundle validated it
        raise CommandError("the spec does not load", "invalid")
    ds = _settings(repo)
    pid = spec.pipeline_id
    envs = enabled_envs(repo, spec)
    sched = schedule(repo, spec)
    files: dict[str, bytes] = {
        f"dags/{dag_name(spec)}.json": _json(dag_json(repo, spec), sort_keys=False),  # the team's key order
        f"src/{pid}/__init__.py": b"",
        f"src/{pid}/task.py": generate_task_py(spec, bundle.sha256, ds.env_var, sched).encode(),
        f"src/{pid}/pipeline_bundle.zip": bundle.data,
        f"tests/{pid}/__init__.py": b"",
        f"tests/{pid}/test_task.py": generate_test_py(spec, envs, ds.env_var).encode(),
        f"docs/pipelines/{pid}.md": pipeline_doc(spec, envs, ds.env_var, sched, dag_name(spec)).encode(),
        f"docs/config-schemas/{pid}.schema.json": _json(config_schema(pid)),
    }
    for env in envs:
        files[f"src/{pid}/config_{env}.json"] = _json(runtime_config(repo, spec, env))
    files |= vendored_files()
    logic = build_lock(spec)["hashes"]["logic_sha"]
    manifest = {
        "layout": "dataops",
        "pipeline_id": pid,
        "dag_id": dag_name(spec),
        "spec_version": spec.pipeline.metadata.spec_version,
        "logic_sha": logic,
        "bundle_sha256": bundle.sha256,
        "environments": envs,
        "image": None,
        "files": {k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())},
    }
    return Rendered(pid, dict(sorted(files.items())), manifest)


# ---------------------------------------------------------------- writing and verifying


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_path(root: Path, pid: str) -> Path:
    return root / "pipelines" / pid / MANIFEST


def _recorded(root: Path) -> dict[str, dict[str, str]]:
    """pipeline id → files its last build recorded (every pipeline: the vendored library is shared)."""
    out: dict[str, dict[str, str]] = {}
    for m in sorted((root / "pipelines").glob(f"*/{MANIFEST}")):
        try:
            files = json.loads(m.read_text(encoding="utf-8")).get("files", {})
        except json.JSONDecodeError:
            files = {}
        out[m.parent.name] = files if isinstance(files, dict) else {}
    return out


def _owned_dirs(pid: str) -> tuple[str, ...]:
    return (f"src/{pid}/", f"tests/{pid}/", f"{VENDOR_ROOT}/")


def _present(root: Path, pid: str) -> list[str]:
    out: list[str] = []
    for d in _owned_dirs(pid):
        base = root / d
        if base.is_dir():
            out += [p.relative_to(root).as_posix() for p in base.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc")]  # fmt: skip
    return sorted(out)


def build_dataops(repo: Repo, pipeline_dir: Path) -> dict[str, Any]:
    if repo.root is None:  # pragma: no cover - the CLI found the repository
        raise CommandError("not inside a pipelines repository", "no_repo")
    root = repo.root
    r = render_dataops(repo, pipeline_dir)
    pid = r.pipeline_id
    recorded = _recorded(root)
    known = {f for files in recorded.values() for f in files}
    candidates = set(r.files) | set(_present(root, pid))
    foreign = sorted(
        rel
        for rel in candidates
        if (root / rel).is_file() and rel not in known and (root / rel).read_bytes() != r.files.get(rel)
    )
    if foreign:
        raise CommandError(
            f"refusing to overwrite files the factory did not generate: {', '.join(foreign[:8])}"
            + (" …" if len(foreign) > 8 else "")
            + f" (a hand-written pipeline named {pid}? rename the spec, or move these files away)",
            "conflict",
        )
    previous = recorded.get(pid, {})
    others = {f for p, files in recorded.items() if p != pid for f in files}
    for rel in sorted(set(previous) - set(r.files) - others):
        (root / rel).unlink(missing_ok=True)  # no longer generated (e.g. an environment was removed)
    for rel, data in r.files.items():
        f = root / rel
        if f.is_file() and f.read_bytes() == data:
            continue
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    _manifest_path(root, pid).write_bytes(r.manifest_bytes)
    return {"layout": "dataops", "dag": r.manifest["dag_id"], "dag_file": f"dags/{r.manifest['dag_id']}.json",
            "task": f"src/{pid}/task.py", "bundle_sha256": r.manifest["bundle_sha256"],
            "environments": r.manifest["environments"], "files": len(r.files),
            "manifest_sha256": hashlib.sha256(canonical_bytes(r.manifest)).hexdigest()}  # fmt: skip


def verify_dataops(repo: Repo, pipeline_dir: Path) -> dict[str, Any]:
    if repo.root is None:  # pragma: no cover
        raise CommandError("not inside a pipelines repository", "no_repo")
    root = repo.root
    pid = pipeline_dir.name
    issues: list[dict[str, str]] = []

    def add(code: str, file: str, msg: str) -> None:
        issues.append({"code": code, "file": file, "msg": msg})

    mpath = _manifest_path(root, pid)
    if not mpath.is_file():
        add("build_missing", mpath.relative_to(root).as_posix(), "no build: run `factory build`")
        return {"ok": False, "issues": issues, "checked": 0}
    try:
        recorded: dict[str, str] = json.loads(mpath.read_text(encoding="utf-8")).get("files", {})
    except json.JSONDecodeError:
        recorded = {}
        add("generated_file_edited", MANIFEST, "the manifest is not valid JSON")
    fresh = render_dataops(repo, pipeline_dir)
    for rel, data in fresh.files.items():
        f = root / rel
        if not f.is_file():
            add("generated_file_missing" if rel in recorded else "build_stale", rel,
                "deleted: rebuild" if rel in recorded else "new output of the changed spec: rebuild")  # fmt: skip
            continue
        have = _sha(f.read_bytes())
        if have == _sha(data):
            continue
        if rel in recorded and have != recorded[rel]:
            add("generated_file_edited", rel, "edited after the build; change the spec and rebuild instead")
        else:
            add("build_stale", rel, "out of date with the spec or framework: rebuild")
    known = {f for files in _recorded(root).values() for f in files}
    for rel in _present(root, pid):
        if rel in fresh.files:
            continue
        if rel in recorded:
            add("build_stale", rel, "no longer generated from the spec: rebuild")
        elif rel not in known:
            add("generated_file_unexpected", rel, "not generated by the factory")
    if not issues and mpath.read_bytes() != fresh.manifest_bytes:
        add("build_stale", MANIFEST, "the manifest is out of date: rebuild")
    return {"ok": not issues, "issues": issues, "checked": len(fresh.files) + 1,
            "manifest_sha256": _sha(fresh.manifest_bytes)}  # fmt: skip
