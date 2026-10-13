"""Evaluate transforms and checks exactly as declared in the locked spec.

Every check produces counts in RuleStats (evidence). Actions:
  quarantine_row  drop the row, record row number + rule (no values)
  reject_package  stop: nothing from the delivery is emitted
  reject_entity   this entity's rows from the delivery are not emitted; other entities are
  fail_sync       stop the sync; state does not advance
  warn            count only
Shadow mode evaluates and counts but never acts.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from . import cel
from .errors import EntityRejected, PackageRejected, SyncFailed
from .values import TypeConversionError, convert, to_check


@dataclass
class RuleStat:
    rule_id: str
    scope: str
    entity: str = ""
    attribute: str = ""
    kind: str = ""
    mode: str = "enforce"
    action: str = ""
    evaluated: int = 0
    failed: int = 0
    outcome: str = "pass"  # pass | warned | quarantined | rejected | failed_sync | shadow_failed


@dataclass
class Stats:
    rules: dict[tuple[str, str, str], RuleStat] = field(default_factory=dict)

    def get(self, rule: dict, scope: str, entity: str = "", attribute: str = "") -> RuleStat:
        key = (rule["id"], entity, attribute)
        st = self.rules.get(key)
        if st is None:
            st = RuleStat(rule["id"], scope, entity, attribute, rule.get("kind", ""), rule.get("mode", "enforce"), rule.get("on_fail", ""))
            self.rules[key] = st
        return st


_PY_LOCK = threading.Lock()


def _python_rule(ref: str):
    module, func = ref.split(":")
    with _PY_LOCK:
        mod = importlib.import_module(f"pf_rules.{module}")
    return getattr(mod, func)


def _secret_bytes(configuration: dict, key: str) -> bytes:
    from .errors import ConfigError

    v = configuration.get(key)
    if not v:
        raise ConfigError(f"configuration key '{key}' is missing or empty")
    return v.encode("utf-8")


# ---------------------------------------------------------------------------- generic check evaluation
def _as_type(x: Any, like: Any) -> Any:
    """Convert x (a value or a range bound) to a type comparable with `like`, the typed attribute value:
    numbers as Decimal; dates and datetimes parsed from ISO text ('T' or space); strings stay strings, but a
    numeric bound never compares with text lexicographically."""
    import datetime as _dt
    import decimal as _dec

    if isinstance(like, bool):
        raise TypeError("booleans have no range")
    if isinstance(like, (int, float, _dec.Decimal)):
        if isinstance(x, bool):
            raise TypeError("boolean bound")
        return _dec.Decimal(str(x))
    if isinstance(like, _dt.datetime):
        if isinstance(x, _dt.datetime):
            d = x
        else:
            d = _dt.datetime.fromisoformat(str(x).strip().replace("Z", "+00:00"))
        if like.tzinfo is not None and d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        if like.tzinfo is None and d.tzinfo is not None:
            d = d.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return d
    if isinstance(like, _dt.date):
        return x if isinstance(x, _dt.date) and not isinstance(x, _dt.datetime) else _dt.date.fromisoformat(str(x).strip()[:10])
    if isinstance(like, str):
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            raise TypeError("numeric bound on text")
        return str(x)
    raise TypeError(f"no range for {type(like).__name__}")


def check_passes(rule: dict, value: Any, row: dict | None, package: dict | None = None) -> bool:
    """Evaluate one check. `value` is the attribute value (typed) or None for row/package checks."""
    k = rule["kind"]
    if k == "not_null":
        return value is not None and value != ""
    if value is None and k in ("regex", "allowed_values", "range", "length"):
        return True  # nullability is the job of not_null / nullable: false
    if k == "regex":
        return re.fullmatch(rule["pattern"], str(value)) is not None
    if k == "allowed_values":
        cv = to_check(value)
        for a in rule["values"]:
            if isinstance(a, bool) or isinstance(cv, bool):  # True is not 1
                if isinstance(a, bool) and isinstance(cv, bool) and a == cv:
                    return True
                continue
            if a == cv or str(a) == str(cv):
                return True
        return False
    if k == "range":
        lo, hi = rule.get("min"), rule.get("max")
        try:
            v = _as_type(value, value)
            return (lo is None or v >= _as_type(lo, value)) and (hi is None or v <= _as_type(hi, value))
        except (TypeError, ValueError, ArithmeticError):
            return False  # value or bound not comparable as the attribute's type: fail closed
    if k == "length":
        n = len(str(value))
        return (rule.get("min") is None or n >= rule["min"]) and (rule.get("max") is None or n <= rule["max"])
    if k in ("expression", "english"):
        expr = rule["expr"] if k == "expression" else rule["compiled_expr"]
        act: dict[str, Any] = {}
        if package is not None:
            act["package"] = package
        if row is not None:
            act["row"] = {c: to_check(v) for c, v in row.items()}
            act["value"] = to_check(value)
        ok, _ = cel.evaluate(expr, act)
        return ok
    if k == "python":
        data = {"package": package} if package is not None else {"row": row, "value": value}
        try:
            reasons = _python_rule(rule["ref"])(data, dict(rule.get("params") or {}))
        except Exception:  # a crashing custom rule fails closed
            return False
        return not reasons
    raise ValueError(f"unknown check kind {k}")


# ---------------------------------------------------------------------------- attribute transforms
def apply_transforms(value: Any, transforms: list[dict], configuration: dict) -> Any:
    for t in transforms:
        k = t["kind"]
        if k == "derive":
            continue  # computed after typing, see compute_derived
        if k == "trim" and isinstance(value, str):
            value = value.strip()
        elif k == "upper" and isinstance(value, str):
            value = value.upper()
        elif k == "lower" and isinstance(value, str):
            value = value.lower()
        elif k == "null_if" and value is not None and str(value) in t["values"]:
            value = None
        elif k == "default" and (value is None or value == ""):
            value = t["value"]
        elif k == "replace" and isinstance(value, str):
            value = re.sub(t["pattern"], t["replacement"], value)
        elif k == "map_values" and value is not None:
            key = str(value)
            if key in t["mapping"]:
                value = t["mapping"][key]
            elif t["unmapped"] == "null":
                value = None
            elif t["unmapped"] == "fail":
                raise TypeConversionError("value not in mapping")
        elif k == "pad_left" and value is not None:
            value = str(value).rjust(t["length"], t["char"])
        elif k == "pseudonymise" and value is not None:
            key = _secret_bytes(configuration, t["key"]["secret"])
            value = hmac.new(key, str(value).encode("utf-8"), hashlib.sha256).hexdigest()
    return value


# ---------------------------------------------------------------------------- one row
@dataclass
class RowResult:
    row: dict | None  # None = quarantined
    failures: list[tuple[str, str]]  # (rule_id, reason code) — no values


def process_row(raw: dict, entity: dict, configuration: dict, stats: Stats) -> RowResult:
    """raw: source column -> raw value. Returns the typed target row, or None if quarantined."""
    name = entity["name"]
    typed: dict[str, Any] = {}
    failures: list[tuple[str, str]] = []
    quarantined = False
    type_failed: set[str] = set()

    def act(rule: dict, scope: str, attribute: str, passed: bool) -> None:
        nonlocal quarantined
        st = stats.get(rule, scope, name, attribute)
        st.evaluated += 1
        if passed:
            return
        st.failed += 1
        if st.mode == "shadow":
            st.outcome = "shadow_failed"
            return
        action = rule.get("on_fail", "quarantine_row")
        if action == "warn":
            st.outcome = "warned"
        elif action == "quarantine_row":
            st.outcome = "quarantined"
            quarantined = True
            failures.append((rule["id"], "check_failed"))
        elif action == "reject_package":
            st.outcome = "rejected"
            raise PackageRejected(rule["id"], f"row check failed on entity {name}")
        elif action == "fail_sync":
            st.outcome = "failed_sync"
            raise SyncFailed(rule["id"], f"row check failed on entity {name}")

    for a in entity["attributes"]:
        src = a.get("source") or a["name"]
        v = raw.get(src)
        try:
            v = apply_transforms(v, a.get("transforms", []), configuration)
            v = convert(v, a) if v is not None else None
        except TypeConversionError:
            rule = {"id": f"type__{a['name']}", "kind": "type", "on_fail": a.get("on_type_error", "quarantine_row"), "mode": "enforce"}
            type_failed.add(a["name"])
            act(rule, "attribute", a["name"], False)
            v = None
        typed[a["name"]] = v

    # derived values (CEL over the typed row)
    for a in entity["attributes"]:
        for t in a.get("transforms", []):
            if t["kind"] == "derive":
                try:
                    typed[a["name"]] = convert(cel.value(t["expr"], {"row": {c: to_check(x) for c, x in typed.items()}, "value": to_check(typed[a["name"]])}), a)
                except (ValueError, TypeConversionError):
                    rule = {"id": f"derive__{a['name']}", "kind": "derive", "on_fail": a.get("on_type_error", "quarantine_row"), "mode": "enforce"}
                    type_failed.add(a["name"])
                    act(rule, "attribute", a["name"], False)

    if quarantined:  # one root cause, one quarantine record: unconvertible values are not re-checked as nulls
        return RowResult(None, failures)
    for a in entity["attributes"]:
        v = typed[a["name"]]
        if not a.get("nullable", True) and a["name"] not in type_failed:
            rule = {"id": f"nullable__{a['name']}", "kind": "not_null", "on_fail": "quarantine_row", "mode": "enforce"}
            act(rule, "attribute", a["name"], v is not None and v != "")
        for c in a.get("checks", []):
            act(c, "attribute", a["name"], check_passes(c, v, typed))
    for c in entity.get("row_checks", []):
        act(c, "row", "", check_passes(c, None, typed))
    return RowResult(None if quarantined else typed, failures)


def escalate_thresholds(entity: dict, stats: Stats, rows_seen: int) -> None:
    """max_failed_pct: too many failing rows turns a row problem into a package rejection."""
    if rows_seen == 0:
        return
    rules = [c for a in entity["attributes"] for c in a.get("checks", [])] + list(entity.get("row_checks", []))
    for c in rules:
        pct = c.get("max_failed_pct")
        if pct is None or c.get("mode") == "shadow":
            continue
        failed = sum(st.failed for (rid, ent, _), st in stats.rules.items() if rid == c["id"] and ent == entity["name"])
        if failed * 100.0 / rows_seen > pct:
            raise PackageRejected(c["id"], f"{failed} of {rows_seen} rows failed (limit {pct}%)")


def entity_check(rule: dict, entity_name: str, stats: Stats, passed: bool, reason: str) -> None:
    st = stats.get(rule, "entity", entity_name)
    st.evaluated += 1
    if passed:
        return
    st.failed += 1
    if st.mode == "shadow":
        st.outcome = "shadow_failed"
        return
    action = rule.get("on_fail", "reject_package")
    if action == "warn":
        st.outcome = "warned"
    elif action == "reject_entity":
        st.outcome = "rejected"
        raise EntityRejected(rule["id"], reason)
    elif action == "fail_sync":
        st.outcome = "failed_sync"
        raise SyncFailed(rule["id"], reason)
    else:
        st.outcome = "rejected"
        raise PackageRejected(rule["id"], reason)


def package_check(rule: dict, stats: Stats, passed: bool, reason: str) -> None:
    st = stats.get(rule, "package")
    st.evaluated += 1
    if passed:
        return
    st.failed += 1
    if st.mode == "shadow":
        st.outcome = "shadow_failed"
        return
    action = rule.get("on_fail", "reject_package")
    if action == "warn":
        st.outcome = "warned"
    elif action == "fail_sync":
        st.outcome = "failed_sync"
        raise SyncFailed(rule["id"], reason)
    else:
        st.outcome = "rejected"
        raise PackageRejected(rule["id"], reason)
