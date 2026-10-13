"""rest.fetch: read every page of a declared API endpoint as one file per page (a lazy file_set stream)."""

from __future__ import annotations

import functools
import io
import json
from collections.abc import Iterator
from typing import Any

from factory_core.errors import ConfigError

from ..apiclient import ApiClient
from ..artifacts import FileStream
from . import op

_EXT = {"json": "json", "jsonl": "jsonl", "csv": "csv", "binary": "bin"}


@op("rest.fetch")
def rest_fetch(ctx: Any) -> list[Any]:
    p = ctx.params
    api = next((a for a in ctx.spec.integrations.apis if a.id == p.api), None)
    binding = ctx.binding.apis.get(p.api)
    if api is None or binding is None:
        raise ConfigError("api_unbound", f"API '{p.api}' or its base_url for this environment is not configured")
    endpoint = next((e for e in api.endpoints if e.id == p.endpoint), None)
    if endpoint is None:
        raise ConfigError("api_endpoint_unknown", f"endpoint '{p.endpoint}' is not declared for API '{p.api}'")

    def secret(name: str, *, purpose: str, refresh: bool = False) -> Any:
        return ctx.secrets.get(name, purpose=purpose, rule_id=ctx.rule.rule_id, refresh=refresh)

    values = {**ctx.vars.strings(), "env": ctx.env, "pipeline_id": ctx.run.pipeline_id}

    def files() -> Iterator[FileStream]:
        client = ApiClient(
            api,
            binding.base_url,
            name=p.api,
            secret=secret,
            extra_ca=ctx.services.extra_ca_pem,
            clock=ctx.services.clock,
            sleep=ctx.services.sleep,
        )
        try:
            for page in client.pages(endpoint, values, validate=p.format in ("json", "jsonl")):
                body = page.body
                if p.format == "jsonl" and isinstance(page.data, dict):
                    rows = next((v for v in page.data.values() if isinstance(v, list)), [])
                    body = b"".join(json.dumps(r, sort_keys=True).encode() + b"\n" for r in rows)
                name = f"{p.endpoint}/page-{page.number:05d}.{_EXT[p.format]}"
                yield FileStream(name, len(body), functools.partial(io.BytesIO, body))
        finally:
            client.close()

    return [files()]
