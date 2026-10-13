"""portal.lookup: ask the self-service portal, validate the answer, map it, guard it, snapshot it (ADR-0009).

The snapshot holds only the mapped entries (destination URI per slot and the declared values), never the raw payload.
It is staged write-once and adopted on a retry, so every later task of the run sees the first answer. The raw
response's sha256 goes to the audit event only.
"""

from __future__ import annotations

from typing import Any

import jmespath
from factory_core.errors import ConfigError, SecurityError
from factory_core.templates import parse_template, render
from factory_spec.artifact_models import PortalSnapshot

from ..apiclient import ApiClient, merged
from . import op


def portal_client(ctx: Any) -> ApiClient:
    portal = ctx.spec.integrations.portal
    binding = ctx.binding.portal
    if portal is None or binding is None:
        raise ConfigError("portal_unbound", "the portal or its base_url for this environment is not configured")

    def secret(name: str, *, purpose: str, refresh: bool = False) -> Any:
        return ctx.secrets.get(name, purpose=purpose, rule_id=ctx.rule.rule_id, refresh=refresh)

    def on_call(**fields: Any) -> None:
        ctx.audit.record("portal_call", rule_id=ctx.rule.rule_id, **fields)
        ctx.services.log.metric("PortalLatency", fields["latency_ms"], "Milliseconds", endpoint=fields["endpoint_id"])

    return ApiClient(
        portal,
        binding.base_url,
        name="portal",
        secret=secret,
        extra_ca=ctx.services.extra_ca_pem,
        clock=ctx.services.clock,
        sleep=ctx.services.sleep,
        on_call=on_call,
    )


def _jsonable(value: Any, what: str) -> Any:
    if isinstance(value, float):
        raise ConfigError("portal_mapping_invalid", f"{what} is a decimal number; map it to text or an integer")
    if isinstance(value, list):
        return [_jsonable(v, what) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v, what) for k, v in value.items()}
    return value


@op("portal.lookup")
def portal_lookup(ctx: Any) -> list[Any]:
    endpoint_id = ctx.params.endpoint
    endpoint = next((e for e in ctx.spec.integrations.portal.endpoints if e.id == endpoint_id), None)
    if endpoint is None:
        raise ConfigError("portal_endpoint_unknown", f"portal endpoint '{endpoint_id}' is not declared")
    values = {**ctx.vars.strings(), "env": ctx.env, "pipeline_id": ctx.run.pipeline_id}
    client = portal_client(ctx)
    try:
        data = merged(list(client.pages(endpoint, values)))
    finally:
        client.close()
    mapping = endpoint.mapping
    for i, guard in enumerate(mapping.expect):
        expected = render(parse_template(guard.equals), values)
        actual = jmespath.search(guard.path, data)
        if actual is None or str(actual) != expected:
            raise SecurityError(
                "portal_expectation_failed",
                f"the portal answer fails guard {i} on {guard.path}; it is not for this run",
            )
    destinations: dict[str, str | None] = {}
    for slot, expr in mapping.destinations.items():
        uri = jmespath.search(expr, data)
        if uri is not None and not isinstance(uri, str):
            raise ConfigError("portal_mapping_invalid", f"the mapping for slot '{slot}' does not give a URI")
        destinations[slot] = uri
    mapped = {name: _jsonable(jmespath.search(expr, data), f"value '{name}'") for name, expr in mapping.values.items()}
    return [PortalSnapshot(endpoint=endpoint_id, destinations=destinations, values=mapped)]
