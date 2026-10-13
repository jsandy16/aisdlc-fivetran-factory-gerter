"""Expression rules (CEL): validate checks and computed names."""

from __future__ import annotations

import re
from typing import Any

from factory_core.errors import ConfigError, SecurityError

from factory_core import cel

from . import CheckFailed, op

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@op("expression")
def run_expression(ctx: Any) -> list[Any]:
    expr = ctx.rule.implementation.cel
    act = ctx.cel_activation()
    if ctx.rule.category == "naming":
        try:
            value = cel.evaluate_value(expr, act)
        except cel.CelEvalError as exc:
            raise ConfigError("expression_failed", str(exc)) from None
        if not isinstance(value, str) or not _SAFE.match(value):
            raise SecurityError("name_unsafe", "the computed name is not a single safe file name")
        return [value]
    passed, _ = cel.evaluate(expr, act)
    if not passed:
        raise CheckFailed("check_failed", f"check {ctx.rule.rule_id} did not hold")
    return []
