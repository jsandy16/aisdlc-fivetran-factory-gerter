"""s3.put, sftp.put, https.put: deliver an artifact to a static slot (integrations.yaml `resolve: {via: static}`).

The slot names an allow-list entry and a path template; the rendered path is resolved like a portal URI, so it must
match that entry's pattern for this run (a `{var}` that renders to `..` or another study is refused as security).
The manifest, when the rule lists one as input, is stored next to the artifact as `<name>.manifest.json`.
"""

from __future__ import annotations

from typing import Any

from factory_core.errors import ConfigError, SecurityError
from factory_core.templates import TemplateError, parse_template, render
from factory_spec.artifact_models import Manifest, PackageInfo
from factory_spec.resolve import Resolution
from factory_spec.sections.destinations import S3Entry
from factory_spec.sections.integrations import StaticResolve

from .. import delivery
from . import op
from .deliver import object_name


def static_resolution(ctx: Any, slot_id: str, kind: str) -> Resolution:
    slot = next((s for s in ctx.spec.integrations.destinations if s.id == slot_id), None)
    if slot is None or not isinstance(slot.resolve, StaticResolve):
        raise ConfigError("destination_slot_unknown", f"slot '{slot_id}' is not a static destination slot")
    entry = next((e for e in ctx.spec.destinations.destinations.get(ctx.env, []) if e.id == slot.resolve.entry), None)
    if entry is None:
        raise ConfigError("destination_entry_unknown", f"entry '{slot.resolve.entry}' is not approved in {ctx.env}")
    if entry.type != kind:
        raise ConfigError("destination_type_mismatch", f"slot '{slot_id}' is {entry.type}; this op writes to {kind}")
    values = {**ctx.vars.strings(), "env": ctx.env, "pipeline_id": ctx.run.pipeline_id}
    try:
        path = render(parse_template(slot.resolve.path), values, path_safe=True).strip("/")
    except TemplateError as exc:
        if exc.code == "template_unsafe_value":
            raise SecurityError("destination_not_allowed", f"the path of slot '{slot_id}' renders unsafely") from None
        raise ConfigError("destination_path_invalid", f"the path of slot '{slot_id}' cannot be rendered") from None
    if isinstance(entry, S3Entry):
        uri = f"s3://{entry.bucket}/{path}/"
    else:
        uri = f"{entry.type}://{entry.host}:{entry.port}/{path}/"
    return delivery.recheck(ctx, slot_id, entry.id, uri)


def _put(ctx: Any, kind: str) -> list[Any]:
    p = ctx.params
    res = static_resolution(ctx, p.slot, kind)
    pkg: PackageInfo = ctx.input_of("package")
    name = object_name(ctx, p.object_name) if p.object_name else pkg.key.rsplit("/", 1)[-1]
    manifest: Manifest | None = ctx.input_of("manifest", required=False)
    receipt = delivery.deliver(
        ctx, res, name, delivery.Package.of(pkg, None), manifest=manifest, sidecars=[], verify=True
    )
    return [receipt]


@op("s3.put")
def s3_put(ctx: Any) -> list[Any]:
    return _put(ctx, "s3")


@op("sftp.put")
def sftp_put(ctx: Any) -> list[Any]:
    return _put(ctx, "sftp")


@op("https.put")
def https_put(ctx: Any) -> list[Any]:
    return _put(ctx, "https")
