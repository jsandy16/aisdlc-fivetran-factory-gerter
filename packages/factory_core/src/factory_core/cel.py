"""CEL (Common Expression Language): safe, side-effect-free expressions for preconditions, validation and naming.

Adapted from pipeline-factory 0.1.0 `pf_runtime/cel.py`: an expression that errors counts as a failure (fail closed),
and int/double equality with null follows the CEL spec (cel-python 0.5.0 raises instead). Adds syntax checking and
extraction of the dotted names an expression references, so specs can be validated without evaluating anything.
"""

from __future__ import annotations

import threading
from typing import Any

import celpy
from celpy import celtypes as _T  # noqa: N812
from celpy.adapter import json_to_cel

_ENV = celpy.Environment()
_CACHE: dict[str, Any] = {}
_LOCK = threading.Lock()


class CelSyntaxError(ValueError):
    pass


class _NullAwareInt(_T.IntType):
    def __eq__(self, other: object) -> bool:
        return False if other is None else bool(super().__eq__(other))

    def __ne__(self, other: object) -> bool:
        return True if other is None else bool(super().__ne__(other))

    __hash__ = _T.IntType.__hash__


class _NullAwareDouble(_T.DoubleType):
    def __eq__(self, other: object) -> bool:
        return False if other is None else bool(super().__eq__(other))

    def __ne__(self, other: object) -> bool:
        return True if other is None else bool(super().__ne__(other))

    __hash__ = _T.DoubleType.__hash__


def to_cel(v: Any) -> Any:
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
    if isinstance(v, list | tuple):
        return _T.ListType([to_cel(x) for x in v])
    if isinstance(v, dict):
        return _T.MapType({_T.StringType(k): to_cel(x) for k, x in v.items()})
    return json_to_cel(v)


def parse(expr: str) -> Any:
    """Return the lark AST, or raise CelSyntaxError with a short message."""
    try:
        return _ENV.compile(expr)
    except Exception as exc:  # celpy raises CELParseError and friends
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise CelSyntaxError(f"not a valid CEL expression: {first[:200]}") from None


def compile_expr(expr: str) -> Any:
    with _LOCK:
        prg = _CACHE.get(expr)
        if prg is None:
            prg = _ENV.program(parse(expr))
            _CACHE[expr] = prg
        return prg


def evaluate(expr: str, activation: dict[str, Any]) -> tuple[bool, Any]:
    """(passed, result_or_error_name). Non-boolean or errored results fail."""
    prg = compile_expr(expr)
    act = {k: to_cel(v) for k, v in activation.items()}
    try:
        res = prg.evaluate(act)
    except Exception as exc:
        return False, type(exc).__name__
    if isinstance(res, _T.BoolType):
        return bool(res), bool(res)
    if isinstance(res, Exception):
        return False, type(res).__name__
    return False, "non_boolean_result"


class CelEvalError(ValueError):
    """Evaluation failed (unknown field, type error, ...); the message names the error type, never values."""


def from_cel(v: Any) -> Any:
    """CEL value -> plain JSON-like Python value."""
    if v is None:
        return None
    if isinstance(v, _T.BoolType):
        return bool(v)
    if isinstance(v, _T.IntType | _T.UintType):
        return int(v)
    if isinstance(v, _T.DoubleType):
        return float(v)
    if isinstance(v, _T.StringType):
        return str(v)
    if isinstance(v, _T.BytesType):
        return bytes(v)
    if isinstance(v, _T.ListType):
        return [from_cel(x) for x in v]
    if isinstance(v, _T.MapType):
        return {str(k): from_cel(x) for k, x in v.items()}
    return v


def evaluate_value(expr: str, activation: dict[str, Any]) -> Any:
    """The value of an expression (naming rules). Raises CelEvalError instead of returning an error object."""
    prg = compile_expr(expr)
    act = {k: to_cel(v) for k, v in activation.items()}
    try:
        res = prg.evaluate(act)
    except Exception as exc:
        raise CelEvalError(f"expression failed: {type(exc).__name__}") from None
    if isinstance(res, Exception):
        raise CelEvalError(f"expression failed: {type(res).__name__}")
    return from_cel(res)


def referenced_paths(expr: str) -> set[tuple[str, ...]]:
    """Dotted member paths an expression reads, e.g. {('artifacts','listing','count'), ('vars','expected_files')}.

    Only the leading dotted chain is returned for indexed access (`vars.a.b[0]` -> ('vars','a','b')). Function names
    such as `size(...)` are not paths.
    """
    from lark import Token, Tree

    tree = parse(expr)
    found: set[tuple[str, ...]] = set()

    def path_of(node: Any) -> tuple[str, ...] | None:
        if not isinstance(node, Tree) or node.data != "member" or not node.children:
            return None
        child = node.children[0]
        if isinstance(child, Tree) and child.data == "member_dot":
            base = path_of(child.children[0])
            name = child.children[1]
            if base is None or not isinstance(name, Token):
                return None
            return (*base, str(name))
        if isinstance(child, Tree) and child.data == "primary" and child.children:
            ident = child.children[0]
            if isinstance(ident, Tree) and ident.data == "ident" and ident.children:
                return (str(ident.children[0]),)
        return None

    def walk(node: Any, parent: Any) -> None:
        if not isinstance(node, Tree):
            return
        if node.data == "member" and not (isinstance(parent, Tree) and parent.data == "member_dot"):
            p = path_of(node)
            if p is not None:
                found.add(p)
        for c in node.children:
            walk(c, node)

    walk(tree, None)
    return found
