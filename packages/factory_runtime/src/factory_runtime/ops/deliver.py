"""destination.deliver: the package, its manifest and checksum files to one portal-resolved slot (ADR-0024).

The slot's URI comes from the run's destination set, but it is resolved again here against the bundle's allow-list:
a staged document never decides where data goes or which credentials are used. An optional slot the portal left
empty gives a `skipped` receipt.
"""

from __future__ import annotations

from typing import Any

from factory_core.errors import ConfigError
from factory_spec.artifact_models import ChecksumRecord, DestinationSet, Manifest, PackageInfo

from .. import delivery
from . import op


def object_name(ctx: Any, artifact: str | None) -> str:
    if artifact is None:
        raise ConfigError("object_name_missing", f"rule {ctx.rule.rule_id} names no object_name artifact")
    value = ctx.artifacts.get(artifact).value
    if not isinstance(value, str):
        raise ConfigError("object_name_invalid", f"artifact '{artifact}' is not a file name")
    return value


@op("destination.deliver")
def destination_deliver(ctx: Any) -> list[Any]:
    p = ctx.params
    name = object_name(ctx, p.object_name)
    dset: DestinationSet = ctx.input_of("destination_set")
    chosen = dset.destinations.get(p.slot)
    if chosen is None:
        slot = next(s for s in ctx.spec.integrations.destinations if s.id == p.slot)
        if slot.required:
            raise ConfigError("destination_slot_missing", f"slot '{p.slot}' was not resolved for this run")
        return [delivery.skipped(ctx, p.slot, name)]
    res = delivery.recheck(ctx, p.slot, chosen.entry_id, chosen.uri)
    pkg: PackageInfo = ctx.input_of("package")
    manifest: Manifest | None = ctx.input_of("manifest", required=False) if p.manifest else None
    if p.manifest and manifest is None:
        raise ConfigError("input_missing", f"rule {ctx.rule.rule_id} delivers a manifest but has no manifest input")
    checksum: ChecksumRecord | None = ctx.input_of("checksum", required=False)
    receipt = delivery.deliver(
        ctx,
        res,
        name,
        delivery.Package.of(pkg, checksum),
        manifest=manifest,
        sidecars=list(p.sidecars),
        verify=p.verify_after_write,
    )
    return [receipt]
