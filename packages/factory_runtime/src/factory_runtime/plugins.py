"""Custom rules (rules/<rule_id>/impl.py): loaded, verified and run through the plugin API (ADR-0020). Phase 3 M10."""

from __future__ import annotations

from typing import Any

from factory_core.errors import ConfigError


def run_custom_rule(ctx: Any) -> list[Any]:
    raise ConfigError("op_not_implemented", "custom rules are not available in this runtime build yet")
