"""Sizes and durations as people write them in specs ("10GB", "512MiB", "15m", "4h"), stored as integers.

Case-sensitive on purpose: `10gb` or `10Gb` are rejected rather than guessed. `GB` is decimal (10^9), `GiB` binary.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Final

_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s?(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$")
_FACTORS: Final = {
    "B": 1,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "KiB": 2**10,
    "MiB": 2**20,
    "GiB": 2**30,
    "TiB": 2**40,
}
_DURATION = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_RETENTION = re.compile(r"^(\d+)(d|y)$")


def parse_bytes(text: str) -> int:
    """'10GB' -> 10_000_000_000. Bare numbers are rejected: the unit is part of the meaning."""
    if isinstance(text, bool) or not isinstance(text, str):
        raise ValueError("write a size with a unit, e.g. 10GB or 512MiB")
    m = _SIZE.match(text.strip())
    if not m:
        raise ValueError(f"'{text}' is not a size; write e.g. 10GB, 500MB or 512MiB (units are case-sensitive)")
    try:
        value = Decimal(m.group(1)) * _FACTORS[m.group(2)]
    except InvalidOperation as exc:  # pragma: no cover - the regex already guarantees a number
        raise ValueError(f"'{text}' is not a size") from exc
    if value != value.to_integral_value():
        raise ValueError(f"'{text}' is not a whole number of bytes")
    return int(value)


def format_bytes(n: int) -> str:
    for unit in ("TB", "GB", "MB", "KB"):
        f = _FACTORS[unit]
        if n >= f and n % f == 0:
            return f"{n // f}{unit}"
    return f"{n}B"


def parse_duration(text: str) -> int:
    """'1h30m' -> 5400 seconds. Units d, h, m, s, in that order."""
    if isinstance(text, bool) or not isinstance(text, str) or not text:
        raise ValueError("write a duration with units, e.g. 15m, 4h or 1h30m")
    m = _DURATION.match(text.strip())
    if not m or not any(m.groups()):
        raise ValueError(f"'{text}' is not a duration; write e.g. 30s, 15m, 4h or 1d2h")
    d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def format_duration(seconds: int) -> str:
    out = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        q, seconds = divmod(seconds, size)
        if q:
            out.append(f"{q}{unit}")
    return "".join(out) or "0s"


def parse_retention_days(text: str) -> int:
    """'30d' -> 30, '10y' -> 3650 (a year counts as 365 days for comparisons)."""
    if not isinstance(text, str):
        raise ValueError("write a retention period like 30d or 10y")
    m = _RETENTION.match(text.strip())
    if not m:
        raise ValueError(f"'{text}' is not a retention period; write e.g. 30d or 10y")
    n = int(m.group(1))
    return n * 365 if m.group(2) == "y" else n
