"""Portal-resolved destinations against the governance allow-list (ADR-0024). Pure logic, no I/O.

A destination URI from the portal names a *folder*. It is accepted only if exactly one allow-list entry of the run's
environment matches it:
- the URI parses under the strict rules of factory_core.uri (scheme, host, no traversal, no encoding tricks);
- the entry serves the slot, and the slot allows the entry's type;
- the type, the host (or bucket) and the port are equal;
- the folder matches the entry's path pattern, where every {var} equals the run's own value.
No match is a security error (the portal can only *choose* among approved destinations); two matches are a
configuration error. Credentials, KMS keys and egress always come from the matched entry, never from the portal.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from factory_core.errors import ConfigError, SecurityError
from factory_core.pathmatch import match, parse_path_pattern
from factory_core.uri import DestinationUri, parse_destination_uri

from .artifact_models import ResolvedDestination
from .sections.destinations import HttpsEntry, S3Entry, SftpEntry
from .spec import PipelineSpec

Entry = S3Entry | SftpEntry | HttpsEntry
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


@dataclass(frozen=True)
class Resolution:
    slot: str
    entry: Entry
    uri: DestinationUri

    @property
    def folder(self) -> tuple[str, ...]:
        return self.uri.segments

    def as_doc(self) -> ResolvedDestination:
        return ResolvedDestination(
            slot=self.slot, entry_id=self.entry.id, type=self.entry.type, uri=canonical_uri(self.uri)
        )


def canonical_uri(u: DestinationUri) -> str:
    host = u.host if u.port is None or u.type == "s3" else f"{u.host}:{u.port}"
    path = "/".join(u.segments)
    return f"{u.type}://{host}/{path}/" if path else f"{u.type}://{host}/"


def _entry_matches(entry: Entry, u: DestinationUri, values: Mapping[str, str]) -> bool:
    if entry.type != u.type:
        return False
    if isinstance(entry, S3Entry):
        if u.host != entry.bucket:
            return False
        pattern = parse_path_pattern(entry.prefix_pattern, absolute=False)
    else:
        if u.host != entry.host or u.port != entry.port:
            return False
        pattern = parse_path_pattern(entry.path_pattern, absolute=True)
    return match(pattern, u.segments, values)


def resolve_destination(
    spec: PipelineSpec, env: str, slot_id: str, uri_text: str, values: Mapping[str, str]
) -> Resolution:
    slot = next((s for s in spec.integrations.destinations if s.id == slot_id), None)
    if slot is None:
        raise ConfigError("destination_slot_unknown", f"slot '{slot_id}' is not declared in integrations.yaml")
    u = parse_destination_uri(uri_text)  # SecurityError destination_uri_invalid
    if u.type not in slot.allowed_types:
        raise SecurityError("destination_not_allowed", f"slot '{slot_id}' does not allow {u.type} destinations")
    entries = [e for e in spec.destinations.destinations.get(env, []) if slot_id in e.slots]
    hits = [e for e in entries if _entry_matches(e, u, values)]
    if not hits:
        raise SecurityError(
            "destination_not_allowed",
            f"the destination for slot '{slot_id}' does not match any approved destination in {env}",
        )
    if len(hits) > 1:
        raise ConfigError(
            "destination_ambiguous",
            f"slot '{slot_id}' matches {len(hits)} approved destinations in {env}: {sorted(e.id for e in hits)}",
        )
    return Resolution(slot_id, hits[0], u)


def resolve_slots(
    spec: PipelineSpec,
    env: str,
    slots: list[str],
    uris: Mapping[str, str | None],
    values: Mapping[str, str],
) -> dict[str, Resolution]:
    """Resolve every requested slot; a required slot without a URI is a configuration error."""
    declared = {s.id: s for s in spec.integrations.destinations}
    out: dict[str, Resolution] = {}
    for slot_id in slots:
        uri = uris.get(slot_id)
        if not uri:
            if declared.get(slot_id) is not None and declared[slot_id].required:
                raise ConfigError("destination_slot_missing", f"the portal named no destination for slot '{slot_id}'")
            continue
        out[slot_id] = resolve_destination(spec, env, slot_id, uri, values)
    return out


def target_segments(folder: tuple[str, ...], object_name: str) -> tuple[str, ...]:
    """The delivery target: the resolved folder plus one safe file name (never a path)."""
    if not _SAFE_NAME.match(object_name) or object_name in (".", ".."):
        raise SecurityError("object_name_unsafe", "the object name is not a single safe file name")
    return (*folder, object_name)
