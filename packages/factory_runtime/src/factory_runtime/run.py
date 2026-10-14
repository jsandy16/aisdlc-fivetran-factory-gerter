"""Pod entry point: `python -m factory_runtime.run --bundle s3://... --sha256 H --task T --run-key K`.

Exit codes (execution-model §6): 0 ok, 75 transient, 65 data, 78 config, 77 security, 70 internal. One JSON log line per
event on stdout; secrets are never printed (factory_core.jsonlog redacts the serialized line).

`main(argv, overrides=...)` is the in-process API for tests. The console entry point and the Lambda handlers never
pass overrides, so production code cannot redirect AWS endpoints or trust extra CAs.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from types import FrameType
from typing import Any

from factory_core.errors import ConfigError, TransientError, exit_code_for
from factory_core.jsonlog import configure, get_logger, reset_bound

from .config import Overrides, RuntimeConfig, from_environ
from .envcheck import check_environment
from .executor import TaskRequest, classified, execute
from .services import Services
from .state import RunState


class _ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # never print usage or exit from inside the runtime
        raise _ArgError(message)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="factory_runtime.run", add_help=False)
    p.add_argument("--bundle", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--run-key", required=True)
    p.add_argument("--attempt", type=int, default=1)
    p.add_argument("--run-id", default="")
    p.add_argument("--conf", default=None, help="JSON run conf (manual and scheduled runs only)")
    return p


def parse_request(argv: Sequence[str]) -> TaskRequest:
    try:
        a = _parser().parse_args(list(argv))
    except _ArgError as exc:
        raise ConfigError("runtime_usage", f"invalid arguments: {exc}") from None
    conf = None
    if a.conf:
        try:
            conf = json.loads(a.conf)
        except json.JSONDecodeError:
            raise ConfigError("runtime_usage", "--conf is not JSON") from None
        if not isinstance(conf, dict):
            raise ConfigError("runtime_usage", "--conf must be a JSON object")
    return TaskRequest(a.bundle, a.sha256, a.task, a.run_key, a.attempt, a.run_id, conf)


def main(
    argv: Sequence[str],
    *,
    config: RuntimeConfig | None = None,
    overrides: Overrides | None = None,
    state: RunState | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    environ = os.environ if environ is None else environ
    log = get_logger()
    reset_bound()
    cfg: RuntimeConfig | None = None
    try:
        req = parse_request(argv)
        cfg = config or from_environ(environ)
        check_environment(cfg.env, environ)
        services = Services.build(cfg, overrides, state=state, log=log)
        outcome = execute(services, req)
        log.event("run_exit", exit_code=0, status=outcome.status)
        return 0
    except BaseException as exc:  # every failure becomes an exit code; nothing escapes as a traceback
        if isinstance(exc, KeyboardInterrupt):
            exc = TransientError("interrupted", "the task was interrupted")
        err = classified(exc, "run")
        code = exit_code_for(err)
        log.event("run_exit", level="ERROR", exit_code=code, error_class=str(err.error_class), code=err.code)
        if cfg is not None:  # alert routing: generated alarms watch this metric per failure class
            log.metric("TaskFailed", 1, "Count", pipeline_id=cfg.pipeline_id, error_class=str(err.error_class))
        return code


def _sigterm(signum: int, frame: FrameType | None) -> None:
    raise TransientError("terminated", "the pod was asked to stop (eviction or timeout)")


def console() -> None:
    configure(sys.stdout, service="factory-runtime")
    signal.signal(signal.SIGTERM, _sigterm)
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    console()
