"""The SecretProvider interface and secret path rendering (ADR-0008)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from factory_core.errors import ConfigError
from factory_core.templates import parse_template, render

from .value import SecretValue


@dataclass(frozen=True)
class ResolvedRef:
    """A secret reference with its environment-specific location filled in."""

    name: str
    provider: str
    path: str
    key: str | None
    version: str | None
    kind: str


class SecretProvider(Protocol):
    def get(
        self, ref: ResolvedRef, *, purpose: str, rule_id: str | None = None, refresh: bool = False
    ) -> SecretValue: ...


def resolve_ref(name: str, ref: Any, tokens: Mapping[str, str]) -> ResolvedRef:
    """Fill {env}, {team}, {pipeline_id} into the declared path (spec validation allows no other tokens)."""
    try:
        path = render(parse_template(ref.path), dict(tokens), path_safe=True)
    except Exception:
        raise ConfigError("secret_path_invalid", f"the path of secret '{name}' cannot be rendered") from None
    return ResolvedRef(name, ref.provider, path, ref.key, ref.version, ref.kind)
