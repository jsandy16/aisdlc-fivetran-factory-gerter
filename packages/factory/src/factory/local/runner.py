"""`factory run`: run a pipeline on a developer's machine, as close to the deployed run as possible.

Same artefacts as a deployment: the build is rendered in memory, the task commands come from the generated DAG,
and each task's settings are the ones the generated Terraform gives the deployed task (`runtime_env`). Differences:

* compute: every task runs here, in this process (Lambda and pod tasks alike);
* secrets: all from the developer's local file (dev only; refused in ppt and prod);
* stores (`stores="local"`, default): the framework's staging, bundle, audit and run-state stores run on a local
  S3 + DynamoDB endpoint, so nothing needs creating; `stores="aws"` uses the real dev stores;
* data (`data="aws"`, default): sources and destinations are the real dev resources the bindings name, reached
  with the developer's AWS credentials; external services (portal, APIs, SFTP/HTTPS partners, Fivetran) are called
  for real. `data="local"` puts the data buckets on the local endpoint too, seeded from local folders.
Tasks that cannot run here (for example a sync that needs a Fivetran key the developer lacks) can be skipped; the
report says per task whether it ran here, which remote endpoints it called, or that it was skipped.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory_core.envelope import CommandError
from factory_spec.repo import Repo
from factory_spec.secrets import local_secrets_path

from ..gen.build import render
from ..gen.names import Deploy, dag_id, deploy_for
from ..gen.terraform import egress, runtime_env

AIRFLOW = {"{{ (dag_run.conf or {}).get('run_key') or run_id }}": "run_key", "{{ ti.try_number }}": "attempt",
           "{{ run_id }}": "run_id"}  # fmt: skip
STRICT = frozenset({"ppt", "prod", "prd"})


def dag_tasks(src: str) -> list[tuple[str, dict[str, Any], dict[str, str] | None]]:
    """(task_id, runtime payload, pod env_vars) in dependency order, read from generated DAG source."""
    tree = ast.parse(src)
    runtime: dict[str, Any] = {}
    tasks: dict[str, tuple[dict[str, Any], dict[str, str] | None]] = {}
    edges: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "RUNTIME":
            runtime = ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            kw = {k.arg: k.value for k in node.value.keywords}
            if "task_id" in kw:
                tid = ast.literal_eval(kw["task_id"])
                env = ast.literal_eval(kw["env_vars"]) if "env_vars" in kw else None
                tasks[tid] = ({**runtime, "task": tid}, env)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.RShift):
            edges.append((node.left.id, node.right.id))  # type: ignore[attr-defined]
    order: list[str] = []
    while len(order) < len(tasks):
        order += [t for t in tasks if t not in order and all(a in order for a, b in edges if b == t)]
    return [(t, *tasks[t]) for t in order]


def render_airflow(v: Any, values: dict[str, str]) -> Any:
    """The Airflow templates a generated DAG uses, filled in the way Airflow fills them."""
    if not isinstance(v, str):
        return v
    for tmpl, key in AIRFLOW.items():
        v = v.replace(tmpl, values[key])
    return v


class _Trigger:
    """Stands in for MWAA: records which DAG the dispatcher starts, and with which conf."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str, dict[str, Any]]] = []

    def start(self, dag: str, dag_run_id: str, conf: dict[str, Any]) -> Any:
        if any(r == dag_run_id for _, r, _ in self.started):
            return "exists"
        self.started.append((dag, dag_run_id, conf))
        return "created"


@dataclass
class LocalRunOptions:
    env: str = "dev"
    message: dict[str, Any] | None = None  # for sqs / s3_event triggers: the message the queue would deliver
    conf: dict[str, Any] | None = None  # for manual and schedule triggers
    tasks: list[str] | None = None  # only these tasks (the rest of the run is assumed done)
    skip: list[str] = field(default_factory=list)  # tasks that cannot run here (reported as skipped)
    stores: str = "local"  # local | aws
    data: str = "aws"  # aws | local
    seed: dict[str, Path] = field(default_factory=dict)  # data="local": bucket -> folder to upload
    secrets_file: Path | None = None
    workdir: Path | None = None
    # tests only: a CA for TLS stand-ins, a second local endpoint standing in for the dev account, host mapping
    extra_ca_pem: bytes | None = None
    remote_endpoint: str | None = None
    hosts: Callable[[], contextlib.AbstractContextManager[Any]] | None = None


@contextlib.contextmanager
def _local_endpoint() -> Iterator[str]:
    try:
        from moto.server import ThreadedMotoServer
    except ImportError:
        raise CommandError("local stores need the development extras (moto): uv sync --all-packages") from None
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.stop()


def _remote(spec: Any, task: str, env: str) -> list[str]:
    return sorted(egress(spec, env).get(task, []))


def run_local(repo: Repo, pipeline_dir: Path, opts: LocalRunOptions) -> dict[str, Any]:
    from factory_airflow.argv import runtime_argv
    from factory_runtime.config import Overrides, from_environ
    from factory_runtime.run import main as runtime_main
    from factory_runtime.services import Services
    from factory_runtime.state.dynamodb import create_table

    from factory_core import jsonlog
    from factory_runtime import dispatch

    from .routing import RoutedAwsClients

    env = opts.env
    if env in STRICT:
        raise CommandError(f"local runs are for development; '{env}' is never run locally", "security")
    ps = repo.platform.get(env)
    if ps is None or not ps.allow_local_secrets:
        raise CommandError(f"the platform does not allow local runs in '{env}' (allow_local_secrets)", "security")
    built = render(repo, pipeline_dir)
    from factory_spec.load import load_spec

    spec = load_spec(pipeline_dir).spec
    if spec is None:  # pragma: no cover - render validated it
        raise CommandError("the spec does not load", "invalid")
    team, pid = spec.pipeline.metadata.team, spec.pipeline_id
    if env not in built.manifest["environments"]:
        raise CommandError(f"'{env}' is not an enabled environment of {pid}")
    secrets_file = opts.secrets_file or local_secrets_path(team, env)
    deploy: Deploy = deploy_for(ps, team, pid, built.manifest["bundle_sha256"])
    base_env = runtime_env(spec, ps, deploy) | {
        "FACTORY_LOCAL_SECRETS_FILE": str(secrets_file),
        "FACTORY_WORKDIR": str(opts.workdir or Path.home() / ".factory" / "work" / pid),
    }
    the_dag = dag_id(pid, spec.pipeline.metadata.spec_version)
    dag_src = built.files[f"dags/{env}/{the_dag}.py"].decode()
    logs = io.StringIO()
    report: dict[str, Any] = {"pipeline_id": pid, "env": env, "dag_id": the_dag, "stores": opts.stores,
                              "data": opts.data, "secrets": "local file", "tasks": []}  # fmt: skip
    with contextlib.ExitStack() as stack:
        local = stack.enter_context(_local_endpoint()) if (opts.stores == "local" or opts.data == "local") else None
        remote = local if opts.data == "local" else opts.remote_endpoint

        def clients(config: Any, overrides: Any) -> Any:
            from factory_runtime.aws import AwsClients

            if opts.stores == "local" and local is not None:
                return RoutedAwsClients(config, overrides, local_endpoint=local, remote_endpoint=remote)
            plain = AwsClients(config, None)
            plain.endpoint_url = remote
            return plain

        overrides = Overrides(extra_ca_pem=opts.extra_ca_pem, aws_clients=clients)
        cfg = from_environ(base_env)
        boot = Services.build(cfg, overrides)

        def ensure(create: Callable[..., Any], *args: Any, **kw: Any) -> None:  # local stores outlive one run
            try:
                create(*args, **kw)
            except Exception as exc:
                if boot.aws.error_code(exc) not in ("BucketAlreadyOwnedByYou", "ResourceInUseException"):
                    raise

        s3 = boot.aws.client("s3")
        where = {"LocationConstraint": cfg.region}
        if opts.stores == "local":
            for b in (cfg.staging_bucket, cfg.bundle_store, cfg.audit_store):
                ensure(s3.create_bucket, Bucket=b, CreateBucketConfiguration=where)
            ensure(create_table, boot.aws.client("dynamodb"), cfg.run_state_table)
        if opts.data == "local":
            for bucket, folder in sorted(opts.seed.items()):
                ensure(s3.create_bucket, Bucket=bucket, CreateBucketConfiguration=where)
                for f in sorted(p for p in folder.rglob("*") if p.is_file()):
                    s3.put_object(Bucket=bucket, Key=f.relative_to(folder).as_posix(), Body=f.read_bytes())
        bucket, key = deploy.bundle_uri[5:].split("/", 1)
        boot.aws.client("s3").put_object(Bucket=bucket, Key=key,
                                         Body=built.files[f"bundle/{deploy.bundle_sha256}.zip"])  # fmt: skip
        jsonlog.configure(logs, service="factory-run")
        stack.callback(jsonlog.configure, None)
        hosts = opts.hosts() if opts.hosts else contextlib.nullcontext()
        stack.enter_context(hosts)
        trig = spec.pipeline.trigger
        if trig.type in ("sqs", "s3_event") and (trig.type == "s3_event" or trig.mode == "dispatch"):
            if opts.message is None:
                raise CommandError(f"this pipeline starts from a {trig.type} message: pass --message FILE")
            deployed = {"FACTORY_BUNDLE_URI": deploy.bundle_uri, "FACTORY_BUNDLE_SHA256": deploy.bundle_sha256,
                        "FACTORY_DAG_ID": the_dag}  # fmt: skip
            dcfg = from_environ(base_env | deployed)
            body = json.dumps(opts.message, sort_keys=True)
            trigger = _Trigger()
            resp, _ = dispatch.build(Services.build(dcfg, overrides), trigger).handle_batch(
                {"Records": [{"messageId": "local-" + hashlib.sha256(body.encode()).hexdigest()[:12], "body": body}]}
            )
            if resp != {"batchItemFailures": []} or not trigger.started:
                raise CommandError("the dispatcher refused the message (see the run log)", "invalid")
            _, run_id, conf = trigger.started[0]
            run_key = str(conf["run_key"])
        else:
            conf = dict(opts.conf or {})
            run_key = str(conf.get("run_key") or "local-" + hashlib.sha256(
                json.dumps(conf, sort_keys=True).encode()).hexdigest()[:12])  # fmt: skip
            from factory_core.runkey import run_dir

            run_id = run_dir(run_key)
        report["run_key"], report["dag_run_id"] = run_key, run_id
        for task, payload, pod_env in dag_tasks(dag_src):
            entry: dict[str, Any] = {"task": task, "remote_endpoints": _remote(spec, task, env)}
            if (opts.tasks and task not in opts.tasks) or task in opts.skip:
                entry |= {"status": "skipped", "why": "skipped on request" if task in opts.skip else "not selected"}
                report["tasks"].append(entry)
                continue
            rendered = {k: render_airflow(v, {"run_key": run_key, "run_id": run_id, "attempt": "1"})
                        for k, v in payload.items()}  # fmt: skip
            task_env = (pod_env or base_env) | {k: base_env[k] for k in ("FACTORY_LOCAL_SECRETS_FILE",
                                                                          "FACTORY_WORKDIR")}  # fmt: skip
            code = runtime_main(runtime_argv(rendered), config=from_environ(task_env), overrides=overrides,
                                environ={})  # fmt: skip
            entry |= {"status": "ran locally", "exit_code": code}
            report["tasks"].append(entry)
            if code != 0:  # as in Airflow: downstream tasks do not start
                break
    events = [json.loads(x) for x in logs.getvalue().splitlines() if x.strip()]
    report["ok"] = all(t.get("exit_code", 0) == 0 for t in report["tasks"])
    report["log_lines"] = len(events)
    report["errors"] = [{k: e.get(k) for k in ("task", "code", "error_class")} for e in events
                        if e.get("event") == "run_exit" and e.get("exit_code")]  # fmt: skip
    report["_log"] = logs.getvalue()
    return report
