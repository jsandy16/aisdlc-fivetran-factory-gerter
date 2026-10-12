"""CEL (Common Expression Language) evaluation: safe, side-effect free, compiled once per rule.

An expression that errors (e.g. on a null) counts as a failure: rules fail closed.
"""
from __future__ import annotations

import threading
from typing import Any

import celpy

_ENV = celpy.Environment()
_CACHE: dict[str, Any] = {}
_LOCK = threading.Lock()


def compile_expr(expr: str):
    with _LOCK:
        prg = _CACHE.get(expr)
        if prg is None:
            prg = _ENV.program(_ENV.compile(expr))
            _CACHE[expr] = prg
        return prg


def evaluate(expr: str, activation: dict[str, Any]) -> tuple[bool, Any]:
    """Return (passed, result_or_error_name). Non-boolean or errored results fail."""
    prg = compile_expr(expr)
    act = {k: celpy.json_to_cel(v) for k, v in activation.items()}
    try:
        res = prg.evaluate(act)
    except Exception as exc:  # CELEvalError and friends
        return False, type(exc).__name__
    if isinstance(res, celpy.celtypes.BoolType):
        return bool(res), bool(res)
    if isinstance(res, Exception):
        return False, type(res).__name__
    return False, "non_boolean_result"


def value(expr: str, activation: dict[str, Any]) -> Any:
    """Evaluate an expression that computes a value (derive transform). Errors raise ValueError."""
    prg = compile_expr(expr)
    act = {k: celpy.json_to_cel(v) for k, v in activation.items()}
    try:
        res = prg.evaluate(act)
    except Exception as exc:
        raise ValueError(f"derive expression failed ({type(exc).__name__})") from None
    if isinstance(res, Exception):
        raise ValueError(f"derive expression failed ({type(res).__name__})")
    return _from_cel(res)


def _from_cel(v: Any) -> Any:
    if isinstance(v, celpy.celtypes.BoolType):
        return bool(v)
    if isinstance(v, (celpy.celtypes.IntType, celpy.celtypes.UintType)):
        return int(v)
    if isinstance(v, celpy.celtypes.DoubleType):
        return float(v)
    if isinstance(v, celpy.celtypes.StringType):
        return str(v)
    if isinstance(v, celpy.celtypes.ListType):
        return [_from_cel(x) for x in v]
    if isinstance(v, celpy.celtypes.MapType):
        return {str(k): _from_cel(x) for k, x in v.items()}
    if v is None:
        return None
    return str(v)
