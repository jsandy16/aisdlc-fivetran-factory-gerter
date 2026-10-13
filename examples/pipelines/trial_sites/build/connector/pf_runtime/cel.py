"""CEL (Common Expression Language) evaluation: safe, side-effect free, compiled once per rule.

An expression that errors counts as a failure: rules fail closed.

Null handling follows the CEL spec's heterogeneous equality (`1 == null` is false, `1 != null` is true).
cel-python 0.5.0 raises "no such overload" for int/double compared with null, which would make every
`row.n != null && ...` guard fail; the null-aware number types below restore spec behaviour for equality only
(ordering such as `row.n < null` still errors, and therefore fails closed).
"""
from __future__ import annotations

import importlib
import threading
from typing import Any

# cel-python is imported by name through importlib on purpose: its wheel ships no top_level.txt, so the
# Connector SDK's pipreqs check cannot map the `celpy` import to `cel-python` and would resolve it against PyPI,
# where an unrelated package named `CelPy` exists — `fivetran deploy --yes` would then add it to requirements.txt.
# cel-python stays pinned in requirements.txt and `pf build` verifies the import offline (generate/connector.py).
celpy = importlib.import_module("celpy")

_T = importlib.import_module("celpy.celtypes")

_ENV = celpy.Environment()


class _NullAwareInt(_T.IntType):
    def __eq__(self, other: Any) -> bool:
        return False if other is None else super().__eq__(other)

    def __ne__(self, other: Any) -> bool:
        return True if other is None else super().__ne__(other)

    __hash__ = _T.IntType.__hash__


class _NullAwareDouble(_T.DoubleType):
    def __eq__(self, other: Any) -> bool:
        return False if other is None else super().__eq__(other)

    def __ne__(self, other: Any) -> bool:
        return True if other is None else super().__ne__(other)

    __hash__ = _T.DoubleType.__hash__


def to_cel(v: Any) -> Any:
    """Python (JSON-like) value -> CEL value, with null-aware numbers."""
    if v is None:
        return None
    if isinstance(v, bool):
        return _T.BoolType(v)
    if isinstance(v, int):
        return _NullAwareInt(v)
    if isinstance(v, float):
        return _NullAwareDouble(v)
    if isinstance(v, str):
        return _T.StringType(v)
    if isinstance(v, (list, tuple)):
        return _T.ListType([to_cel(x) for x in v])
    if isinstance(v, dict):
        return _T.MapType({_T.StringType(k): to_cel(x) for k, x in v.items()})
    return celpy.json_to_cel(v)
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
    act = {k: to_cel(v) for k, v in activation.items()}
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
    act = {k: to_cel(v) for k, v in activation.items()}
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
