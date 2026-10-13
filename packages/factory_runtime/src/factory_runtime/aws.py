"""AWS clients for the runtime: one place for region, retries, timeouts and the (test-only) endpoint override.

botocore retries throttling and 5xx itself ("standard" mode, 3 attempts); runtime code does not add a second retry
loop around AWS calls. Credentials come from the default chain (Pod Identity / IRSA / Lambda role).
"""

from __future__ import annotations

import threading
from typing import Any

import boto3
from botocore.config import Config

from .config import Overrides, RuntimeConfig

_BOTO_CONFIG = Config(
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=10,
    read_timeout=60,
    tcp_keepalive=True,
)


class AwsClients:
    def __init__(self, config: RuntimeConfig, overrides: Overrides | None = None) -> None:
        self.region = config.region
        self.endpoint_url = overrides.aws_endpoint_url if overrides else None
        self._session = boto3.session.Session(region_name=config.region)
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def session(self) -> Any:
        return self._session

    def client(self, service: str) -> Any:
        with self._lock:
            c = self._clients.get(service)
            if c is None:
                kw: dict[str, Any] = {"config": _BOTO_CONFIG}
                if self.endpoint_url:
                    kw["endpoint_url"] = self.endpoint_url
                c = self._session.client(service, **kw)
                self._clients[service] = c
            return c

    def error_code(self, exc: BaseException) -> str:
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            return str(response.get("Error", {}).get("Code", ""))
        return ""

    def status(self, exc: BaseException) -> int:
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            return int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        return 0

    def precondition_failed(self, exc: BaseException) -> bool:
        return self.status(exc) == 412 or self.error_code(exc) == "PreconditionFailed"
