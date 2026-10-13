"""A stand-in for the Fivetran REST API (v1): connections in a group, start a sync, poll it. Synthetic only.

A started sync finishes after `polls_to_finish` reads of the connection, with the configured `outcome`
(`success`, `failure`, or `never`). Faults: `rate_limit_once` answers 429 with Retry-After once.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

from .certs import TestCA
from .httpserver import Request, Response, StandIn

T0 = datetime(2026, 10, 13, 6, 0, 0, tzinfo=UTC)


def _iso(seconds: int) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class MockFivetran(StandIn):
    def __init__(self, ca: TestCA, *, api_key: str, api_secret: str, hostname: str = "api.fivetran.com") -> None:
        super().__init__(ca, (hostname,))
        self.basic = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        self.connections: dict[str, dict[str, Any]] = {}
        self.outcome = "success"
        self.polls_to_finish = 2
        self.rate_limit_once = False
        self.syncs_started = 0
        self._polls = 0
        self._clock = 100

    def add(self, cid: str, group: str, schema: str, **kw: Any) -> None:
        self.connections[cid] = {
            "id": cid,
            "group_id": group,
            "schema": schema,
            "service": "postgres",
            "paused": False,
            "succeeded_at": _iso(0),
            "failed_at": None,
            "status": {"sync_state": "scheduled", "setup_state": "connected"},
            **kw,
        }

    def _tick(self) -> str:
        self._clock += 60
        return _iso(self._clock)

    def handle(self, req: Request) -> Response:
        if req.headers.get("authorization") != f"Basic {self.basic}":
            return Response(401, {"code": "AuthFailed", "message": "Invalid credentials"})
        if self.rate_limit_once:
            self.rate_limit_once = False
            return Response(429, {"code": "TooManyRequests"}, {"Retry-After": "1"})
        parts = req.path.strip("/").split("/")
        if req.method == "GET" and len(parts) == 4 and parts[:2] == ["v1", "groups"] and parts[3] == "connections":
            items = [c for c in self.connections.values() if c["group_id"] == parts[2]]
            return Response(200, {"code": "Success", "data": {"items": items, "next_cursor": None}})
        if len(parts) >= 3 and parts[:2] == ["v1", "connections"]:
            conn = self.connections.get(parts[2])
            if conn is None:
                return Response(404, {"code": "NotFound_Connection"})
            if req.method == "POST" and parts[3:] == ["sync"]:
                self.syncs_started += 1
                self._polls = 0
                conn["status"]["sync_state"] = "syncing"
                return Response(200, {"code": "Success", "message": "Sync has been successfully triggered"})
            if req.method == "GET" and len(parts) == 3:
                if conn["status"]["sync_state"] == "syncing":
                    self._polls += 1
                    if self._polls >= self.polls_to_finish and self.outcome != "never":
                        conn["status"]["sync_state"] = "scheduled"
                        conn["succeeded_at" if self.outcome == "success" else "failed_at"] = self._tick()
                return Response(200, {"code": "Success", "data": conn})
        return Response(404, {"code": "NotFound"})
