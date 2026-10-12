"""Transforms and checks at every scope: package, entity, row and attribute.

Every check shares one anatomy: id, kind, parameters, on_fail, mode, description.
Custom logic comes in three forms:
  * expression — a CEL expression (safe, side-effect free, typed at build time)
  * python     — a reviewed function in the pipeline's rules/ folder
  * english    — plain-English intent, compiled to a CEL expression at build time
                 (pf rules compile), frozen in rules.lock.yaml and accepted by a person.
                 English is never evaluated at runtime.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import Field, StringConstraints, model_validator

from .common import Identifier, Mode, OnFailEntity, OnFailPackage, OnFailRow, Regex, SecretRef, StrictModel

RuleId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=63)]
PythonRef = Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]*:[a-z_][a-z0-9_]*$")]


class Example(StrictModel):
    """A hand-worked case the owner labels BEFORE seeing any compiled logic."""

    input: dict[str, Any] = Field(description="row (or package) values the rule sees")
    expected: Literal["pass", "fail"]


class _Check(StrictModel):
    id: RuleId
    description: str = Field(min_length=3, description="Plain-language intent, shown in evidence and reviews")
    mode: Mode = "enforce"
    tags: list[str] = Field(default_factory=list)


class _English(StrictModel):
    kind: Literal["english"]
    text: str = Field(min_length=10, description="The rule in words; compiled at build time, never run")
    examples: list[Example] = Field(default_factory=list, description="At least 3 owner-labelled cases")


class _Expression(StrictModel):
    kind: Literal["expression"]
    expr: str = Field(min_length=1, description="CEL expression that is true when the data passes")


class _Python(StrictModel):
    kind: Literal["python"]
    ref: PythonRef = Field(description="module:function inside the pipeline's rules/ folder")
    params: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- package (structural) checks
class _PackageCheck(_Check):
    on_fail: OnFailPackage = "reject_package"


class FileNamePattern(_PackageCheck):
    kind: Literal["file_name_pattern"]
    pattern: Regex = Field(description="Regex the package file name (not the path) must fully match")


class FileExtension(_PackageCheck):
    kind: Literal["file_extension"]
    allowed: list[Annotated[str, StringConstraints(pattern=r"^\.[A-Za-z0-9.]+$")]] = Field(min_length=1)


class FileSize(_PackageCheck):
    kind: Literal["file_size"]
    min_bytes: int = Field(default=1, ge=0)
    max_bytes: int | None = Field(default=None, gt=0)


class Checksum(_PackageCheck):
    kind: Literal["checksum"]
    algorithm: Literal["md5", "sha256"] = "sha256"
    sidecar: str = Field(default="{name}.{algorithm}", description="Sidecar file next to the package; {name} and {algorithm} are replaced")


class PackageType(_PackageCheck):
    kind: Literal["package_type"]
    allowed: list[Literal["zip", "gzip", "csv", "json", "jsonl", "text"]] = Field(min_length=1)


class RequiredMembers(_PackageCheck):
    kind: Literal["required_members"]
    patterns: list[Regex] = Field(min_length=1, description="Each regex must match at least one member path")


class ForbiddenMembers(_PackageCheck):
    kind: Literal["forbidden_members"]
    patterns: list[Regex] = Field(min_length=1)


class MemberCount(_PackageCheck):
    kind: Literal["member_count"]
    min: int = Field(default=1, ge=0)
    max: int | None = Field(default=None, ge=1)


class FolderStructure(_PackageCheck):
    kind: Literal["folder_structure"]
    path_pattern: Regex = Field(description="Regex the package's folder (relative to the source root) must fully match")


class NotEmpty(_PackageCheck):
    kind: Literal["not_empty"]


class DuplicatePackage(_PackageCheck):
    kind: Literal["duplicate_package"]
    lookback_packages: int = Field(default=500, ge=1, le=5000)


class Encoding(_PackageCheck):
    kind: Literal["encoding"]
    encoding: str = "utf-8"


class PackageExpression(_PackageCheck, _Expression):
    """CEL over `package`: name, path, size, sha256, members (list of paths), groups (named regex groups)."""


class PackagePython(_PackageCheck, _Python):
    pass


class PackageEnglish(_PackageCheck, _English):
    pass


PackageCheck = Annotated[
    Union[
        FileNamePattern, FileExtension, FileSize, Checksum, PackageType, RequiredMembers, ForbiddenMembers,
        MemberCount, FolderStructure, NotEmpty, DuplicatePackage, Encoding, PackageExpression, PackagePython,
        PackageEnglish,
    ],
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- package transforms
class Unzip(StrictModel):
    kind: Literal["unzip"]
    max_members: int = Field(default=1000, ge=1)
    max_total_bytes: int = Field(default=4 * 1024**3, ge=1)
    max_ratio: int = Field(default=200, ge=1, description="Largest allowed uncompressed/compressed ratio (zip-bomb guard)")


class Gunzip(StrictModel):
    kind: Literal["gunzip"]
    max_total_bytes: int = Field(default=4 * 1024**3, ge=1)


class RenameMembers(StrictModel):
    kind: Literal["rename_members"]
    pattern: Regex
    to: str = Field(description="Replacement using named groups, e.g. '{entity}.csv'")


PackageTransform = Annotated[Union[Unzip, Gunzip, RenameMembers], Field(discriminator="kind")]


# --------------------------------------------------------------------------- entity checks (whole entity, before emit)
class _EntityCheck(_Check):
    on_fail: OnFailEntity = "reject_package"


class RowCount(_EntityCheck):
    """Rows per delivery (file sources) or per sync (database). For incremental tables `min` applies only to the
    initial full load, because a later sync legitimately reads no changed rows."""
    kind: Literal["row_count"]
    min: int = Field(default=1, ge=0)
    max: int | None = Field(default=None, ge=1)


class UniqueKey(_EntityCheck):
    kind: Literal["unique"]
    columns: list[Identifier] = Field(min_length=1)


class ControlCount(_EntityCheck):
    kind: Literal["control_count"]
    control_member: Regex = Field(description="Regex for the control/trailer member inside the package")
    count_pattern: Regex = Field(default=r"(\d+)", description="Regex whose first group is the expected row count")


EntityCheck = Annotated[Union[RowCount, UniqueKey, ControlCount], Field(discriminator="kind")]


# --------------------------------------------------------------------------- row and attribute checks
class _RowCheck(_Check):
    on_fail: OnFailRow = "quarantine_row"
    max_failed_pct: float | None = Field(
        default=None, ge=0, le=100, description="Escalate to reject_package when more than this % of rows fail"
    )


class NotNull(_RowCheck):
    kind: Literal["not_null"]


class RegexCheck(_RowCheck):
    kind: Literal["regex"]
    pattern: Regex


class AllowedValues(_RowCheck):
    kind: Literal["allowed_values"]
    values: list[Any] = Field(min_length=1)


class RangeCheck(_RowCheck):
    kind: Literal["range"]
    min: float | str | None = None
    max: float | str | None = None

    @model_validator(mode="after")
    def _one_bound(self) -> "RangeCheck":
        if self.min is None and self.max is None:
            raise ValueError("range needs min, max or both")
        return self


class LengthCheck(_RowCheck):
    kind: Literal["length"]
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=1)


class RowExpression(_RowCheck, _Expression):
    """CEL over `row` (all attributes after transforms) and `value` (this attribute, if attribute-scoped)."""


class RowPython(_RowCheck, _Python):
    pass


class RowEnglish(_RowCheck, _English):
    pass


AttributeCheck = Annotated[
    Union[NotNull, RegexCheck, AllowedValues, RangeCheck, LengthCheck, RowExpression, RowPython, RowEnglish],
    Field(discriminator="kind"),
]
RowCheck = Annotated[Union[RowExpression, RowPython, RowEnglish], Field(discriminator="kind")]


# --------------------------------------------------------------------------- attribute transforms
class Trim(StrictModel):
    kind: Literal["trim"]


class Upper(StrictModel):
    kind: Literal["upper"]


class Lower(StrictModel):
    kind: Literal["lower"]


class NullIf(StrictModel):
    kind: Literal["null_if"]
    values: list[str] = Field(min_length=1)


class Default(StrictModel):
    kind: Literal["default"]
    value: Any


class Replace(StrictModel):
    kind: Literal["replace"]
    pattern: Regex
    replacement: str = ""


class MapValues(StrictModel):
    kind: Literal["map_values"]
    mapping: dict[str, Any] = Field(min_length=1)
    unmapped: Literal["keep", "null", "fail"] = "fail"


class PadLeft(StrictModel):
    kind: Literal["pad_left"]
    length: int = Field(ge=1)
    char: str = Field(default="0", min_length=1, max_length=1)


class Pseudonymise(StrictModel):
    """Keyed HMAC-SHA256; the key is a secret, never in the spec. Same input + key -> same output."""

    kind: Literal["pseudonymise"]
    key: SecretRef


class Derive(StrictModel):
    """Value computed by a CEL expression over `row` and `value`."""

    kind: Literal["derive"]
    expr: str = Field(min_length=1)


AttributeTransform = Annotated[
    Union[Trim, Upper, Lower, NullIf, Default, Replace, MapValues, PadLeft, Pseudonymise, Derive],
    Field(discriminator="kind"),
]

ENGLISH_KINDS = {"english"}
