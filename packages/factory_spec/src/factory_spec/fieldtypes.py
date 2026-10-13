"""Constrained scalar types used across the spec. Sizes and durations are stored as integers (no floats in hashes)."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any, Literal

import jmespath
from factory_core.templates import TemplateError, parse_template
from factory_core.uri import valid_hostname
from pydantic import AfterValidator, BeforeValidator, PlainSerializer, StringConstraints, WithJsonSchema
from pydantic_core import PydanticCustomError

from factory_core import cel, units

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=63)]
EnvName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]{1,9}$")]
SemVer = Annotated[str, StringConstraints(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")]
Handle = Annotated[str, StringConstraints(pattern=r"^@[A-Za-z0-9][A-Za-z0-9-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*)?$")]
RequirementId = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9]*-\d+$")]
S3Bucket = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")]
KmsAlias = Annotated[str, StringConstraints(pattern=r"^alias/[A-Za-z0-9/_-]{1,250}$")]
AwsRegion = Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(-gov)?-[a-z]+-\d$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Glob = Annotated[str, StringConstraints(pattern=r"^[^\s\\]+$", max_length=256)]
ResourceName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")]
HostKeyFingerprint = Annotated[str, StringConstraints(pattern=r"^SHA256:[A-Za-z0-9+/]{43}=?$")]
Attestation = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9-]{2,63}$")]
# relative S3 folder, ends with "/", no empty, "." or ".." segments: incoming/ or studies/inbound/
PrefixRoot = Annotated[str, StringConstraints(pattern=r"^([A-Za-z0-9_][A-Za-z0-9._-]*/)+$", max_length=512)]
TableName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$", max_length=255)
]


def _regex(v: str) -> str:
    try:
        re.compile(v)
    except re.error as exc:
        raise ValueError(f"not a valid regular expression: {exc}") from exc
    return v


def coded_error(code: str, reason: str) -> PydanticCustomError:
    """A validation error whose type is a registered issue code; the loader reports it under that code."""
    return PydanticCustomError(code, "{reason}", {"reason": reason})


def _cel(v: str) -> str:
    try:
        cel.parse(v)
    except cel.CelSyntaxError as exc:
        raise coded_error("cel_invalid", str(exc)) from None
    return v


def _jmespath(v: str) -> str:
    try:
        jmespath.compile(v)
    except jmespath.exceptions.JMESPathError as exc:
        raise coded_error("jmespath_invalid", f"not a valid JMESPath expression: {str(exc).splitlines()[0]}") from None
    return v


def _template(v: str) -> str:
    try:
        parse_template(v)
    except TemplateError as exc:
        raise coded_error(exc.code, str(exc)) from None
    return v


def _hostname(v: str) -> str:
    if v != v.lower() or not valid_hostname(v):
        raise ValueError("not a valid lower-case DNS host name (no IP literals, no trailing dot)")
    return v


def _https_base_url(v: str) -> str:
    m = re.match(r"^https://([a-z0-9.-]+)(?::(\d{1,5}))?(/[A-Za-z0-9._/-]*)?$", v)
    if not m or not valid_hostname(m.group(1)):
        raise ValueError("must be an https:// URL with a DNS host name, without query or credentials")
    return v


def _bytes(v: Any) -> int:
    if isinstance(v, int) and not isinstance(v, bool):
        raise ValueError("write a size with a unit, e.g. 10GB (a bare number is ambiguous)")
    return units.parse_bytes(v)


def _duration(v: Any) -> int:
    if isinstance(v, int) and not isinstance(v, bool):
        raise ValueError("write a duration with a unit, e.g. 15m or 4h")
    return units.parse_duration(v)


def _retention(v: str) -> str:
    units.parse_retention_days(v)
    return v


def _money(v: Any) -> Decimal:
    if isinstance(v, float | bool):
        raise ValueError('write amounts as text with two decimals, e.g. "2.00"')
    try:
        d = Decimal(str(v))
    except InvalidOperation as exc:
        raise ValueError("not an amount") from exc
    if d < 0:
        raise ValueError("amounts cannot be negative")
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _sorted_unique(v: list[Any]) -> list[Any]:
    return sorted(set(v))


Regex = Annotated[str, AfterValidator(_regex)]
CelExpr = Annotated[str, StringConstraints(min_length=1, max_length=2000), AfterValidator(_cel)]
JmesPath = Annotated[str, StringConstraints(min_length=1, max_length=500), AfterValidator(_jmespath)]
TemplateStr = Annotated[str, StringConstraints(min_length=1, max_length=500), AfterValidator(_template)]
Hostname = Annotated[str, AfterValidator(_hostname)]
HttpsBaseUrl = Annotated[str, AfterValidator(_https_base_url)]
RetentionPeriod = Annotated[
    str, AfterValidator(_retention), WithJsonSchema({"type": "string", "pattern": r"^\d+(d|y)$"})
]

ByteSize = Annotated[
    int,
    BeforeValidator(_bytes),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^\d+(\.\d+)?\s?(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$",
            "examples": ["10GB", "512MiB"],
        }
    ),
]
Duration = Annotated[
    int,
    BeforeValidator(_duration),
    WithJsonSchema(
        {"type": "string", "pattern": r"^(\d+d)?(\d+h)?(\d+m)?(\d+s)?$", "examples": ["15m", "4h", "1h30m"]}
    ),
]
Money = Annotated[
    Decimal,
    BeforeValidator(_money),
    PlainSerializer(lambda d: f"{d:.2f}", return_type=str),
    WithJsonSchema({"type": "string", "pattern": r"^\d+(\.\d{1,2})?$", "examples": ["2.00"]}),
]

DigestAlgorithm = Literal["md5", "sha256", "sha512"]
SortedDigests = Annotated[list[DigestAlgorithm], AfterValidator(_sorted_unique)]
SortedStrings = Annotated[list[str], AfterValidator(_sorted_unique)]
SortedIds = Annotated[list[Identifier], AfterValidator(_sorted_unique)]
