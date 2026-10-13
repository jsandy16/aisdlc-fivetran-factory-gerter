"""Everything a task needs from the outside world, built once per invocation (and replaceable in tests)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from factory_core.jsonlog import JsonLogger, get_logger

from .aws import AwsClients
from .config import Overrides, RuntimeConfig
from .state import RunState
from .state.dynamodb import DynamoRunState


@dataclass
class Services:
    config: RuntimeConfig
    aws: AwsClients
    state: RunState
    log: JsonLogger
    overrides: Overrides | None = None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Any] = time.sleep
    _cache: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        config: RuntimeConfig,
        overrides: Overrides | None = None,
        *,
        state: RunState | None = None,
        log: JsonLogger | None = None,
    ) -> Services:
        aws = AwsClients(config, overrides)
        clock = overrides.clock if overrides and overrides.clock else time.time
        sleep = overrides.sleep if overrides and overrides.sleep else time.sleep
        st = state or DynamoRunState(aws.client("dynamodb"), config.run_state_table, config.pipeline_id, clock=clock)
        return cls(config, aws, st, log or get_logger(), overrides, clock, sleep)

    def cached(self, key: str, factory: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = factory()
        return self._cache[key]

    @property
    def extra_ca_pem(self) -> bytes | None:
        return self.overrides.extra_ca_pem if self.overrides else None
