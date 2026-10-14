"""fivetran.sync: start the sync of a Fivetran-managed database source and wait for it (decision Q10: REST API).

- The connection is found by name in the environment's Fivetran group (the source id is the connection's schema
  name); exactly one must match.
- The platform's Fivetran API key is a Secrets Manager reference in the runtime configuration, fetched through the
  audited provider; it is sent as HTTP Basic auth and never logged.
- The baseline (`succeeded_at`/`failed_at` before the sync) is kept in run-state, so a retried task does not start a
  second sync when the first one is still running or already finished.
- Finished: `succeeded_at` moves past the baseline (success) or `failed_at` does (data, 65). Not finished within the
  timeout: transient (75). Refused credentials: configuration (78). 429 and 5xx are retried with backoff.
The Airflow wrapper (deferrable, for the `mwaa` engine) comes in Phase 5 and drives the same API.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

import httpx
from factory_core.errors import ConfigError, DataError, ErrorClass, SecurityError, TransientError
from factory_core.retry import Backoff, call_with_retry, classify, classify_http_status, parse_retry_after
from factory_spec.artifact_models import SyncReport

from ..secrets.aws_sm import AwsSecretsManagerProvider
from ..secrets.provider import ResolvedRef
from ..secrets.router import AuditedProvider
from ..secrets.value import SecretValue
from ..tls import http_client, read_capped, ssl_context
from . import op

API = "https://api.fivetran.com"
POLL_FIRST = 15.0
POLL_MAX = 60.0


class _RetryableError(TransientError):
    def __init__(self, status: int, retry_after: float | None) -> None:
        super().__init__("fivetran_unavailable", f"the Fivetran API answered {status}")
        self.retry_after = retry_after


class FivetranApi:
    def __init__(self, client: httpx.Client, basic: SecretValue, *, sleep: Any, clock: Any) -> None:
        self.client = client
        self.basic = basic
        self.sleep = sleep
        self.clock = clock

    def close(self) -> None:
        self.client.close()

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        def once(attempt: int) -> dict[str, Any]:
            try:
                with (
                    self.basic.reveal() as basic,
                    self.client.stream(
                        method,
                        API + path,
                        headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
                        content=json.dumps(body).encode() if body is not None else None,
                    ) as res,
                ):
                    if 300 <= res.status_code < 400:
                        raise ConfigError("redirect_refused", "the Fivetran API redirected; not followed")
                    if res.status_code in (401, 403):
                        raise ConfigError("fivetran_auth_refused", "Fivetran refused the platform API key")
                    if res.status_code == 404:
                        raise ConfigError("fivetran_not_found", f"Fivetran has no {path.split('/')[2]} at that id")
                    if classify_http_status(res.status_code) is ErrorClass.TRANSIENT:
                        raise _RetryableError(res.status_code, parse_retry_after(res.headers.get("Retry-After")))
                    if res.status_code >= 400:
                        raise ConfigError("fivetran_request_rejected", f"Fivetran refused {method} ({res.status_code})")
                    data = read_capped(res)
            except httpx.TransportError as exc:
                cls = classify(exc)
                if cls is ErrorClass.SECURITY:
                    raise SecurityError("tls_untrusted", "the Fivetran API certificate is not trusted") from exc
                raise TransientError("fivetran_unavailable", f"cannot reach Fivetran ({type(exc).__name__})") from exc
            try:
                doc = json.loads(data)
            except json.JSONDecodeError:
                raise ConfigError("fivetran_response_invalid", "the Fivetran API answered with no JSON") from None
            if not isinstance(doc, dict) or not isinstance(doc.get("data", {}), dict):
                raise ConfigError("fivetran_response_invalid", "unexpected Fivetran API answer")
            return doc

        return call_with_retry(
            once,
            policy=Backoff(attempts=6, base=2.0, maximum=60.0, deadline=600.0),
            retry_after=lambda exc: getattr(exc, "retry_after", None),
            sleep=self.sleep,
            clock=self.clock,
        )

    def connection_id(self, group: str, name: str) -> str:
        found: list[str] = []
        cursor: str | None = None
        for _ in range(100):
            q = f"?limit=100&cursor={cursor}" if cursor else "?limit=100"
            data = self.call("GET", f"/v1/groups/{group}/connections{q}").get("data", {})
            found += [str(c["id"]) for c in data.get("items", []) if c.get("schema") == name]
            cursor = data.get("next_cursor")
            if not cursor:
                break
        if len(found) != 1:
            code = "fivetran_connection_unknown" if not found else "fivetran_connection_ambiguous"
            raise ConfigError(code, f"{len(found)} Fivetran connections are named '{name}' in the group")
        return found[0]

    def connection(self, cid: str) -> dict[str, Any]:
        data: dict[str, Any] = self.call("GET", f"/v1/connections/{cid}").get("data", {})
        return data


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _after(value: Any, baseline: Any) -> bool:
    now, before = _ts(value), _ts(baseline)
    return now is not None and (before is None or now > before)


def _api(ctx: Any) -> FivetranApi:
    cfg = ctx.services.config
    if not cfg.fivetran_api_secret:
        raise ConfigError("fivetran_not_configured", "the runtime has no Fivetran API key reference")
    ref = ResolvedRef("fivetran_api", "aws_secrets_manager", cfg.fivetran_api_secret, None, None, "api_key")
    provider = AuditedProvider(AwsSecretsManagerProvider(ctx.services.aws), ctx.audit.record, ctx.services)
    fields = provider.get(ref, purpose="fivetran sync", rule_id=ctx.rule.rule_id).json_fields()
    if "api_key" not in fields or "api_secret" not in fields:
        raise ConfigError("secret_format_invalid", "the Fivetran API secret needs api_key and api_secret")
    with fields["api_key"].reveal() as k, fields["api_secret"].reveal() as s:
        basic = SecretValue(base64.b64encode(f"{k}:{s}".encode()).decode(), "fivetran_basic_auth")
    client = http_client(ssl_context(extra_ca=ctx.services.extra_ca_pem), connect_timeout=10, read_timeout=60)
    return FivetranApi(client, basic, sleep=ctx.services.sleep, clock=ctx.services.clock)


@op("fivetran.sync")
def fivetran_sync(ctx: Any) -> list[Any]:
    p = ctx.params
    src = next((s for s in ctx.spec.integrations.sources if s.id == p.connection and s.type == "database"), None)
    binding = ctx.binding.sources.get(p.connection)
    if src is None or binding is None or not binding.fivetran_group:
        raise ConfigError("source_unbound", f"database source '{p.connection}' has no Fivetran group in {ctx.env}")
    api = _api(ctx)
    try:
        cid = api.connection_id(binding.fivetran_group, p.connection)
        conn = api.connection(cid)
        if conn.get("paused"):
            raise ConfigError("fivetran_connection_paused", f"the Fivetran connection of '{p.connection}' is paused")
        if (conn.get("status") or {}).get("setup_state") == "broken":
            raise ConfigError("fivetran_connection_broken", f"the Fivetran connection of '{p.connection}' is broken")
        state = ctx.services.state
        key = f"FIVETRAN#{ctx.task_id}#{ctx.rule.rule_id}"
        base = {"succeeded_at": conn.get("succeeded_at"), "failed_at": conn.get("failed_at"), "triggered": False}
        _, rec = state.put_if_absent(ctx.run.run_dir, key, base)
        baseline = rec.doc
        if _after(conn.get("succeeded_at"), baseline["succeeded_at"]):
            return [_report(p.connection, cid, baseline, conn, triggered=bool(baseline.get("triggered")))]
        syncing = (conn.get("status") or {}).get("sync_state") == "syncing"
        if not (baseline.get("triggered") and syncing):
            api.call("POST", f"/v1/connections/{cid}/sync", {"force": False})
            state.put(ctx.run.run_dir, key, {**baseline, "triggered": True})
            ctx.log("fivetran_sync_started", connection=p.connection)
        if not p.wait:
            return [SyncReport(connection=p.connection, connection_id=cid, outcome="triggered", triggered=True,
                               previous_succeeded_at=baseline["succeeded_at"])]  # fmt: skip
        # a poll budget (Lambda behind an Airflow reschedule sensor) ends this invocation early as "pending" (75);
        # the next poke continues from the stored baseline without starting another sync
        budget = ctx.services.config.poll_budget_s
        deadline = ctx.services.clock() + (min(p.timeout, budget) if budget else p.timeout)
        delay = POLL_FIRST
        while True:
            conn = api.connection(cid)
            if _after(conn.get("succeeded_at"), baseline["succeeded_at"]):
                return [_report(p.connection, cid, baseline, conn, triggered=True)]
            if _after(conn.get("failed_at"), baseline["failed_at"]):
                raise DataError("fivetran_sync_failed", f"the Fivetran sync of '{p.connection}' failed")
            if ctx.services.clock() + delay > deadline:
                if budget and budget < p.timeout:
                    raise TransientError("fivetran_sync_pending", f"the sync of '{p.connection}' is still running")
                raise TransientError("fivetran_sync_timeout", f"the sync of '{p.connection}' did not finish in time")
            ctx.services.sleep(delay)
            delay = min(POLL_MAX, delay * 1.5)
    finally:
        api.close()


def _report(name: str, cid: str, baseline: dict[str, Any], conn: dict[str, Any], *, triggered: bool) -> SyncReport:
    return SyncReport(
        connection=name,
        connection_id=cid,
        outcome="succeeded",
        triggered=triggered,
        previous_succeeded_at=baseline.get("succeeded_at"),
        succeeded_at=conn.get("succeeded_at"),
    )
