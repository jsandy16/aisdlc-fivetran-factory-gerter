"""The built-in op library: one function per catalog op, registered by name.

An op receives the RuleContext and returns its outputs in the order of the rule's `outputs`. Ops that write large
staged objects themselves (package.zip) add the output name to `ctx.staged`; the executor stages every other
document whose durability is staged or final.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from factory_core.errors import ConfigError, DataError

OpFn = Callable[[Any], list[Any]]
REGISTRY: dict[str, OpFn] = {}

_MODULES = (
    "expr",
    "naming",
    "validate",
    "sqs",
    "portal",
    "resolve",
    "rest",
    "s3_list",
    "s3_download",
    "package",
    "manifest",
    "checksum",
    "deliver",
    "static_put",
    "s3_copy",
    "fivetran",
)


class CheckFailed(DataError):  # noqa: N818 - the domain term used in the docs
    """A check did not hold (validate op, expression, precondition). Only these follow `on_failure.error_class`."""


def op(name: str) -> Callable[[OpFn], OpFn]:
    def register(fn: OpFn) -> OpFn:
        if name in REGISTRY:
            raise RuntimeError(f"op {name} registered twice")
        REGISTRY[name] = fn
        return fn

    return register


def load_all() -> dict[str, OpFn]:
    for m in _MODULES:
        try:
            importlib.import_module(f"{__name__}.{m}")
        except ModuleNotFoundError as exc:
            if exc.name != f"{__name__}.{m}":
                raise
    return REGISTRY


def lookup(name: str) -> OpFn:
    load_all()
    fn = REGISTRY.get(name)
    if fn is None:
        raise ConfigError("op_not_implemented", f"op {name} is not implemented by this runtime")
    return fn
