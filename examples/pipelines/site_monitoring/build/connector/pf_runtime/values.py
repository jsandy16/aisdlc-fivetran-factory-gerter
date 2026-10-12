"""Type conversion to Fivetran column types, and the value forms used by checks and by emission."""
from __future__ import annotations

import datetime as dt
import decimal
import json
from typing import Any

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
            return value if isinstance(value, str) else (value.isoformat() if hasattr(value, "isoformat") else str(value))
        if t in ("INT", "LONG"):
            if isinstance(value, bool):
                raise TypeConversionError("boolean is not an integer")
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                if not value.is_integer():
                    raise TypeConversionError("fractional value")
                return int(value)
            s = str(value).strip()
            d = decimal.Decimal(s)
            if d != d.to_integral_value():
                raise TypeConversionError("fractional value")
            n = int(d)
            if t == "INT" and not (-(2**31) <= n < 2**31):
                raise TypeConversionError("out of INT range")
            if not (-(2**63) <= n < 2**63):
                raise TypeConversionError("out of LONG range")
            return n
        if t == "DOUBLE":
            if isinstance(value, bool):
                raise TypeConversionError("boolean is not a number")
            f = float(value) if not isinstance(value, str) else float(value.strip())
            if f != f or f in (float("inf"), float("-inf")):
                raise TypeConversionError("not a finite number")
            return f
        if t == "DECIMAL":
            d = value if isinstance(value, decimal.Decimal) else decimal.Decimal(str(value).strip())
            if not d.is_finite():
                raise TypeConversionError("not a finite decimal")
            q = decimal.Decimal(1).scaleb(-attr["scale"])
            d = d.quantize(q, rounding=decimal.ROUND_HALF_UP)
            if len(d.as_tuple().digits) > attr["precision"]:
                raise TypeConversionError("exceeds declared precision")
            return d
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
