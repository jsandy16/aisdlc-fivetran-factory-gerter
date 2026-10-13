"""Type conversion to Fivetran column types, and the value forms used by checks and by emission."""
from __future__ import annotations

import base64
import datetime as dt
import decimal
import json
import re
from typing import Any

# Strict ASCII grammars: no '_' separators, no non-ASCII digits, no exponents in integers, bounded exponents.
_INT = re.compile(r"[+-]?[0-9]+")
_DEC = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
_FLOAT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?")

TRUE = {"true", "t", "yes", "y", "1"}
FALSE = {"false", "f", "no", "n", "0"}


class TypeConversionError(ValueError):
    """Raised without the offending value in the message."""


def convert(value: Any, attr: dict) -> Any:
    """Convert a raw value (string from a file, or a driver-native value) to the attribute's type.

    Returns a Python value: str, int, float, Decimal, bool, date, datetime (tz-aware for UTC_DATETIME), dict/list.
    """
    if value is None:
        return None
    t = attr["type"]
    try:
        if t == "STRING":
            if isinstance(value, str):
                return value
            if isinstance(value, (bytes, bytearray, memoryview)):  # driver binary: text if UTF-8, else base64
                b = bytes(value)
                try:
                    return b.decode("utf-8")
                except UnicodeDecodeError:
                    return "base64:" + base64.b64encode(b).decode("ascii")
            if isinstance(value, (dict, list)):
                return json.dumps(value, sort_keys=True, default=str)
            if isinstance(value, (dt.date, dt.datetime, dt.time)):
                return value.isoformat()
            if isinstance(value, (bool, int, float, decimal.Decimal)):
                return str(value)
            raise TypeConversionError(f"unsupported value for STRING ({type(value).__name__})")
        if t in ("INT", "LONG"):
            if isinstance(value, bool):
                raise TypeConversionError("boolean is not an integer")
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                if not value.is_integer():
                    raise TypeConversionError("fractional value")
                return int(value)
            if isinstance(value, decimal.Decimal):
                if value != value.to_integral_value():
                    raise TypeConversionError("fractional value")
                n = int(value)
            else:
                s = str(value).strip()
                if not _INT.fullmatch(s):
                    raise TypeConversionError("not an integer")
                n = int(s)
            if t == "INT" and not (-(2**31) <= n < 2**31):
                raise TypeConversionError("out of INT range")
            if not (-(2**63) <= n < 2**63):
                raise TypeConversionError("out of LONG range")
            return n
        if t == "DOUBLE":
            if isinstance(value, bool):
                raise TypeConversionError("boolean is not a number")
            if isinstance(value, str):
                if not _FLOAT.fullmatch(value.strip()):
                    raise TypeConversionError("not a number")
                value = value.strip()
            f = float(value)
            if f != f or f in (float("inf"), float("-inf")):
                raise TypeConversionError("not a finite number")
            return f
        if t == "DECIMAL":
            with decimal.localcontext() as ctx:
                ctx.prec = attr["precision"] + 2  # the default context (28 digits) is too small for DECIMAL(38,x)
                if isinstance(value, decimal.Decimal):
                    d = value
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    d = decimal.Decimal(str(value))
                else:
                    s = str(value).strip()
                    if not _DEC.fullmatch(s):
                        raise TypeConversionError("not a decimal")
                    d = decimal.Decimal(s)
                if not d.is_finite():
                    raise TypeConversionError("not a finite decimal")
                q = decimal.Decimal(1).scaleb(-attr["scale"])
                exact = d.quantize(q, rounding=decimal.ROUND_HALF_UP)
                if exact != d:  # never round source data silently
                    raise TypeConversionError("more decimal places than the declared scale")
                if len(exact.as_tuple().digits) > attr["precision"]:
                    raise TypeConversionError("exceeds declared precision")
                return exact
        if t == "BOOLEAN":
            if isinstance(value, bool):
                return value
            s = str(value).strip().lower()
            if s in TRUE:
                return True
            if s in FALSE:
                return False
            raise TypeConversionError("not a boolean")
        if t == "NAIVE_DATE":
            if isinstance(value, dt.datetime):
                return value.date()
            if isinstance(value, dt.date):
                return value
            s = str(value).strip()
            fmt = attr.get("input_format")
            return dt.datetime.strptime(s, fmt).date() if fmt else dt.date.fromisoformat(s)
        if t in ("NAIVE_DATETIME", "UTC_DATETIME"):
            if isinstance(value, dt.datetime):
                d_ = value
            elif isinstance(value, dt.date):
                d_ = dt.datetime(value.year, value.month, value.day)
            else:
                s = str(value).strip()
                fmt = attr.get("input_format")
                d_ = dt.datetime.strptime(s, fmt) if fmt else dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if t == "NAIVE_DATETIME":
                return d_.replace(tzinfo=None) if d_.tzinfo is None else d_.astimezone(dt.timezone.utc).replace(tzinfo=None)
            return d_.replace(tzinfo=dt.timezone.utc) if d_.tzinfo is None else d_.astimezone(dt.timezone.utc)
        if t == "JSON":
            if isinstance(value, (dict, list)):
                return value
            if isinstance(value, (bytes, bytearray, memoryview)):
                value = bytes(value).decode("utf-8")
            return json.loads(value)
    except TypeConversionError:
        raise
    except (ValueError, TypeError, decimal.InvalidOperation, json.JSONDecodeError, OverflowError) as exc:
        raise TypeConversionError(f"cannot convert to {t} ({type(exc).__name__})") from None
    raise TypeConversionError(f"unsupported type {t}")


def to_emit(value: Any) -> Any:
    """Python value -> what the Connector SDK receives (ISO strings for dates, strings for decimals)."""
    if value is None or isinstance(value, (str, bool, int, float, dict, list)):
        return value
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def to_check(value: Any) -> Any:
    """Python value -> form used by CEL and comparisons: numbers stay numbers, dates become ISO strings."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return to_emit(value)
    return value
