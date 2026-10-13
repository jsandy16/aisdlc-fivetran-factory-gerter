"""destination.resolve: accept the portal's destinations only if each matches exactly one approved entry (ADR-0024)."""

from __future__ import annotations

import hashlib
from typing import Any

from factory_core.errors import FactoryError
from factory_spec.artifact_models import DestinationSet, PortalSnapshot
from factory_spec.resolve import resolve_slots

from . import op


@op("destination.resolve")
def destination_resolve(ctx: Any) -> list[Any]:
    snap: PortalSnapshot = ctx.input_of("portal_snapshot")
    slots = list(ctx.params.slots)
    try:
        res = resolve_slots(ctx.spec, ctx.env, slots, snap.destinations, ctx.vars.strings())
    except FactoryError as exc:
        for slot in slots:
            uri = snap.destinations.get(slot) or ""
            ctx.audit.record(
                "destination_resolution",
                rule_id=ctx.rule.rule_id,
                slot=slot,
                outcome="rejected",
                code=exc.code,
                uri_sha256=hashlib.sha256(uri.encode()).hexdigest(),
            )
        raise
    for slot, r in res.items():
        ctx.audit.record(
            "destination_resolution",
            rule_id=ctx.rule.rule_id,
            slot=slot,
            outcome="accepted",
            entry_id=r.entry.id,
            destination_type=r.entry.type,
            uri_sha256=hashlib.sha256((snap.destinations.get(slot) or "").encode()).hexdigest(),
        )
    return [DestinationSet(env=ctx.env, destinations={s: r.as_doc() for s, r in res.items()})]
