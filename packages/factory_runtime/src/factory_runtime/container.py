"""Run a pipeline inside a DataOps container (the dataops layout, ADR-0026). The generated task.py uses it:

    pipeline = Pipeline.load(PIPELINE_DIR, BUNDLE_SHA256)
    return pipeline.run_each(run_pipeline)        # run_pipeline(run) calls run.task(...) and task.step(...)

What one container start does, by trigger:
- s3_event / sqs: poll the queue. Each message is checked and snapshotted exactly as the dispatcher Lambda does it
  (envelope, fields, run key, write-once custody snapshot, duplicates acknowledged, poison quarantined), then its run
  executes here. The message is deleted when the run finished (or was a duplicate or poison); a failed run leaves it
  for redelivery, so the queue's maxReceiveCount and dead-letter queue bound the attempts.
- schedule: one run; its data_interval_start is the start time (to the minute, UTC).
- manual: one run with the conf in FACTORY_RUN_CONF (JSON with run_key and the declared fields).
The exit code is the most serious failure of the start (execution-model §6); 0 when every run succeeded.

Configuration: config_<env>.json next to task.py, <env> from the variable the DataOps DAG sets (project_env). It
holds bindings and store names only, never secrets.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from factory_core.errors import ConfigError, ErrorClass, FactoryError, exit_code_for
from factory_core.jsonlog import bind, configure, get_logger
from factory_core.runkey import run_dir, run_key_hash
from pydantic import ValidationError

from .bundles import LoadedBundle, load_local_bundle
from .config import Overrides, RuntimeConfig
from .dispatch import Dispatcher
from .envcheck import check_environment
from .executor import TaskOutcome, TaskRequest, TaskSession, check_environment_binding, classified, pinned
from .messages import Poison, quarantine
from .ops import NoWork
from .services import Services
from .state import RunState

BUNDLE_FILE = "pipeline_bundle.zip"
RUN_CONF_ENV = "FACTORY_RUN_CONF"
_SEVERITY = {ErrorClass.SECURITY: 5, ErrorClass.INTERNAL: 4, ErrorClass.CONFIG: 3, ErrorClass.DATA: 2,
             ErrorClass.TRANSIENT: 1}  # fmt: skip


def config_path(pipeline_dir: Path, env: str) -> Path:
    return pipeline_dir / f"config_{env}.json"


@dataclass(frozen=True)
class PollSettings:
    queue_url: str
    max_messages: int = 10
    wait_seconds: int = 1
    max_runs: int = 100


def load_settings(
    pipeline_dir: Path, env_var: str, environ: Mapping[str, str]
) -> tuple[RuntimeConfig, PollSettings | None]:
    env = environ.get(env_var, "").strip()
    if not env:
        raise ConfigError("environment_unset", f"the {env_var} variable is not set")
    path = config_path(pipeline_dir, env)
    if not path.is_file():
        raise ConfigError("environment_not_bound", f"{path.name} does not exist: the pipeline is not built for {env}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        raw = {**doc["runtime"], "env": env}
        if environ.get("FACTORY_WORKDIR"):
            raw["workdir"] = environ["FACTORY_WORKDIR"]
        cfg = RuntimeConfig.model_validate(raw)
    except (ValueError, KeyError, TypeError, ValidationError):
        raise ConfigError("runtime_config_invalid", f"{path.name} is not a valid runtime configuration") from None
    if doc.get("environment") != env:
        raise ConfigError("runtime_config_invalid", f"{path.name} belongs to another environment")
    poll = doc.get("poll")
    return cfg, PollSettings(**poll) if isinstance(poll, dict) else None


class Task:
    """`with run.task("t1_copy") as task: task.step("r_read_event") ...`: one task of the spec, step by step."""

    def __init__(self, run: Run, task_id: str) -> None:
        self.run = run
        self.task_id = task_id
        self.outcome: TaskOutcome | None = None
        self._stack = ExitStack()
        self._session: TaskSession | None = None

    def __enter__(self) -> Task:
        r, p = self.run, self.run.pipeline
        cfg = p.services.config
        self._stack.enter_context(bind(pipeline_id=cfg.pipeline_id, team=cfg.team, env=cfg.env, task_id=self.task_id,
                                       attempt=r.attempt, run_key=run_key_hash(r.run_key)))  # fmt: skip
        try:
            check_environment_binding(r.bundle, p.services, self.task_id)
            self._stack.enter_context(bind(bundle_sha=r.bundle.sha256[:12], image_digest=cfg.image_digest))
            req = TaskRequest("local", r.bundle.sha256, self.task_id, r.run_key, r.attempt, r.rdir, r.conf)
            self._session = TaskSession(p.services, req, r.bundle, r.pin, r.rdir)
        except BaseException:
            self._stack.close()
            raise
        return self

    def step(self, rule_id: str) -> None:
        if self._session is None:
            raise ConfigError("task_not_started", "use the task in a with block")
        self._session.run_rule(rule_id)

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool:
        session = self._session
        with self._stack:
            if session is None:  # pragma: no cover - __enter__ failed
                return False
            if exc is None:
                self.outcome = session.complete()
                self.run.outcomes.append(self.outcome)
                return False
            if isinstance(exc, NoWork):
                self.outcome = session.no_work(exc)
                self.run.outcomes.append(self.outcome)
                return True
            session.fail(exc)
        return False  # pragma: no cover - fail() raises


@dataclass
class Run:
    pipeline: Pipeline
    run_key: str
    conf: dict[str, Any] | None
    attempt: int
    rdir: str = ""
    bundle: LoadedBundle = field(init=False)
    pin: Any = field(init=False)
    outcomes: list[TaskOutcome] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rdir = run_dir(self.run_key)
        self.bundle, self.pin = pinned(self.pipeline.services, self.pipeline.bundle, self.rdir)

    def task(self, task_id: str) -> Task:
        return Task(self, task_id)


class _InProcess:
    """The dispatcher's trigger, here: a validated message's run executes in this container."""

    def __init__(self, pipeline: Pipeline, body: Callable[[Run], None]) -> None:
        self.pipeline = pipeline
        self.body = body
        self.attempt = 1

    def start(self, dag_id: str, dag_run_id: str, conf: dict[str, Any]) -> Literal["created", "exists"]:
        self.body(Run(self.pipeline, conf["run_key"], None, self.attempt))
        return "created"


class Pipeline:
    def __init__(self, services: Services, bundle: LoadedBundle, poll: PollSettings | None,
                 environ: Mapping[str, str]) -> None:  # fmt: skip
        self.services = services
        self.bundle = bundle
        self.poll = poll
        self.environ = environ
        self.worst: FactoryError | None = None

    @classmethod
    def load(
        cls,
        pipeline_dir: Path,
        bundle_sha256: str,
        *,
        env_var: str = "project_env",
        environ: Mapping[str, str] | None = None,
        overrides: Overrides | None = None,
        state: RunState | None = None,
    ) -> Pipeline:
        environ = os.environ if environ is None else environ
        if overrides is None:
            configure(sys.stdout, service="factory-container")
        cfg, poll = load_settings(pipeline_dir, env_var, environ)
        check_environment(cfg.env, environ, cfg.allowed_env)
        services = Services.build(cfg, overrides, state=state, log=get_logger())
        bundle = load_local_bundle(services.aws, cfg, pipeline_dir / BUNDLE_FILE, bundle_sha256)
        return cls(services, bundle, poll, environ)

    # ------------------------------------------------------------------ outcomes

    def _failed(self, exc: BaseException, where: str) -> None:
        err = classified(exc, where)
        cfg = self.services.config
        self.services.log.event("run_failed", level="ERROR", error_class=str(err.error_class), code=err.code)
        self.services.log.metric(
            "TaskFailed", 1, "Count", pipeline_id=cfg.pipeline_id, error_class=str(err.error_class)
        )
        if self.worst is None or _SEVERITY[err.error_class] > _SEVERITY[self.worst.error_class]:
            self.worst = err

    def exit_code(self) -> int:
        code = 0 if self.worst is None else exit_code_for(self.worst)
        self.services.log.event("run_exit", exit_code=code)
        return code

    # ------------------------------------------------------------------ entry

    def run_each(self, body: Callable[[Run], None]) -> int:
        """Run `body` once per run this start owns (see the module docstring); returns the exit code."""
        trig = self.bundle.spec.pipeline.trigger
        try:
            if trig.type in ("sqs", "s3_event"):
                self._poll_queue(body)
            else:
                conf = self._conf(trig.type)
                key = conf.get("run_key") if trig.type == "manual" else f"scheduled__{conf['data_interval_start']}"
                if not isinstance(key, str) or not key:
                    raise ConfigError("trigger_conf_invalid", "a manual run needs run_key in its conf")
                body(Run(self, key, conf, 1))
        except BaseException as exc:
            self._failed(exc, "run")
        return self.exit_code()

    def _conf(self, trigger: str) -> dict[str, Any]:
        if trigger == "schedule":
            return {"data_interval_start": time.strftime("%Y-%m-%dT%H:%M:00+00:00", time.gmtime(self.services.clock()))}
        try:
            conf = json.loads(self.environ.get(RUN_CONF_ENV, ""))
        except json.JSONDecodeError:
            raise ConfigError("trigger_conf_invalid", f"{RUN_CONF_ENV} is not JSON") from None
        if not isinstance(conf, dict):
            raise ConfigError("trigger_conf_invalid", f"{RUN_CONF_ENV} must be a JSON object")
        return conf

    def _poll_queue(self, body: Callable[[Run], None]) -> None:
        if self.poll is None:
            raise ConfigError("runtime_config_invalid", "the configuration has no queue to poll")
        trigger = _InProcess(self, body)
        dispatcher = Dispatcher(self.services, self.bundle, trigger)
        sqs = self.services.aws.client("sqs")
        handled, seen = 0, set()
        while handled < self.poll.max_runs:
            res = sqs.receive_message(
                QueueUrl=self.poll.queue_url,
                MaxNumberOfMessages=min(10, self.poll.max_messages, self.poll.max_runs - handled),
                WaitTimeSeconds=self.poll.wait_seconds,
                MessageSystemAttributeNames=["ApproximateReceiveCount"],
            )
            messages = [m for m in res.get("Messages", []) if m["MessageId"] not in seen]
            if not messages:  # empty, or only messages that already failed in this start: leave them for later
                break
            for m in messages:
                seen.add(m["MessageId"])
                handled += 1
                trigger.attempt = int(m.get("Attributes", {}).get("ApproximateReceiveCount", 1))
                if self._message(dispatcher, m):
                    sqs.delete_message(QueueUrl=self.poll.queue_url, ReceiptHandle=m["ReceiptHandle"])
        self.services.log.event("queue_polled", messages=handled)

    def _message(self, dispatcher: Dispatcher, m: Mapping[str, Any]) -> bool:
        """True when the message is done with (delete it); False to leave it for redelivery."""
        message_id, raw = str(m.get("MessageId", "")), str(m.get("Body", ""))
        try:
            snaps = dispatcher.snapshots(raw)
            if not snaps:
                self.services.log.event("message_ignored")
            for snap in snaps:
                dispatcher.dispatch_one(snap, message_id)
            return True
        except Poison as p:
            quarantine(self.services, message_id, raw, p.code)
            return True
        except BaseException as exc:
            self._failed(exc, "run")
            return False
