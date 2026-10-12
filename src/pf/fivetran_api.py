"""Minimal Fivetran REST API client (https://api.fivetran.com/v1), look-up-before-write (the API has no
idempotency keys). Auth: FIVETRAN_API_KEY = base64("<key>:<secret>"), the same value `fivetran deploy` takes.
PF_FIVETRAN_API_BASE overrides the base URL (used by the local mock in tests and dry runs)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .envelope import PfError

DEFAULT_BASE = "https://api.fivetran.com/v1"


class FivetranApi:
    def __init__(self, api_key_b64: str | None = None, base: str | None = None, sleep=time.sleep):
        self.key = api_key_b64 or os.environ.get("FIVETRAN_API_KEY", "")
        if not self.key:
            raise PfError("FIVETRAN_API_KEY is not set (base64 of key:secret)", "config")
        self.base = (base or os.environ.get("PF_FIVETRAN_API_BASE") or DEFAULT_BASE).rstrip("/")
        self.sleep = sleep

    def request(self, method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        if query:
            from urllib.parse import urlencode

            url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(1, 6):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Basic {self.key}", "Accept": "application/json;version=2", "Content-Type": "application/json",
                "User-Agent": "pipeline-factory"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 5:
                    retry_after = exc.headers.get("Retry-After")
                    self.sleep(min(float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt, 60))
                    continue
                detail = exc.read()[:300].decode("utf-8", errors="replace")
                raise PfError(f"Fivetran API {method} {path} -> HTTP {exc.code}: {detail}", "fivetran_api") from None
            except urllib.error.URLError as exc:
                if attempt < 5:
                    self.sleep(2 ** attempt)
                    continue
                raise PfError(f"Fivetran API unreachable: {exc.reason}", "fivetran_api") from None
        raise PfError("Fivetran API retries exhausted", "fivetran_api")

    def _paged(self, path: str, query: dict | None = None):
        cursor = None
        while True:
            resp = self.request("GET", path, query={**(query or {}), "limit": 1000, "cursor": cursor})
            data = resp.get("data", {})
            yield from data.get("items", [])
            cursor = data.get("next_cursor")
            if not cursor:
                return

    def find_group(self, name: str) -> dict | None:
        return next((g for g in self._paged("/groups") if g.get("name") == name), None)

    def find_connection(self, group_id: str, schema: str) -> dict | None:
        return next((c for c in self._paged(f"/groups/{group_id}/connections", {"schema": schema}) if c.get("schema") == schema), None)

    def get_destination(self, destination_id: str) -> dict:
        return self.request("GET", f"/destinations/{destination_id}").get("data", {})


def ensure_destination(api: FivetranApi, settings: dict, dry_run: bool = False) -> dict:
    """Create the group + Managed Data Lake destination if missing; never modify an existing one (immutable
    fields like bucket/prefix/role/Glue are compared and reported)."""
    d = settings["destination"]
    group = api.find_group(d["group_name"])
    req = d["create_destination_request"]
    if group is None:
        if dry_run:
            return {"action": "create", "group": d["group_name"], "request": req}
        group = api.request("POST", "/groups", {"name": d["group_name"]})["data"]
        dest = api.request("POST", "/destinations", {**req, "group_id": group["id"]})["data"]
        return {"action": "created", "group_id": group["id"], "destination": dest.get("id", group["id"]),
                "setup_tests": dest.get("setup_tests", [])}
    current = api.get_destination(group["id"])
    cfg = current.get("config", {})
    drift = {k: {"spec": v, "fivetran": cfg[k]} for k, v in req["config"].items() if k in cfg and cfg[k] != v}
    if current.get("service") not in (None, "managed_data_lake"):
        drift["service"] = {"spec": "managed_data_lake", "fivetran": current.get("service")}
    return {"action": "exists", "group_id": group["id"], "drift": drift}


def apply_schedule(api: FivetranApi, group_name: str, connection: str, schedule: dict, dry_run: bool = False) -> dict:
    """`fivetran deploy` creates connections paused at 360 minutes and never changes the schedule on redeploy,
    so the schedule from pipeline.yaml is applied here, every deploy."""
    body: dict[str, Any] = {"paused": bool(schedule.get("paused", False)), "sync_frequency": int(schedule.get("sync_frequency", 360)),
                            "schedule_type": "auto"}
    if schedule.get("daily_sync_time"):
        body["daily_sync_time"] = schedule["daily_sync_time"]
    if dry_run:
        return {"action": "patch", "connection": connection, "body": body}
    group = api.find_group(group_name)
    if group is None:
        raise PfError(f"Fivetran destination '{group_name}' not found", "fivetran_api")
    conn = api.find_connection(group["id"], connection)
    if conn is None:
        raise PfError(f"connection '{connection}' not found in '{group_name}' after deploy", "fivetran_api")
    api.request("PATCH", f"/connections/{conn['id']}", {**body, "run_setup_tests": False})
    return {"action": "patched", "connection_id": conn["id"], "body": body}


def drift(api: FivetranApi, group_name: str, connection: str, schedule: dict) -> dict:
    group = api.find_group(group_name)
    if group is None:
        return {"status": "missing_destination"}
    conn = api.find_connection(group["id"], connection)
    if conn is None:
        return {"status": "missing_connection"}
    want = {"paused": bool(schedule.get("paused", False)), "sync_frequency": int(schedule.get("sync_frequency", 360))}
    have = {"paused": conn.get("paused"), "sync_frequency": int(conn.get("sync_frequency", 0) or 0)}
    diff = {k: {"spec": want[k], "fivetran": have[k]} for k in want if want[k] != have[k]}
    return {"status": "drift" if diff else "in_sync", "connection_id": conn.get("id"), "diff": diff,
            "sync_state": (conn.get("status") or {}).get("sync_state"), "setup_state": (conn.get("status") or {}).get("setup_state")}
