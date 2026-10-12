"""sttm.yaml — entities (target tables) and the source-to-target mapping of every attribute."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import FivetranType, Identifier, Regex, StrictModel
from .rules import AttributeCheck, AttributeTransform, EntityCheck, RowCheck


class FileFormat(StrictModel):
    type: Literal["csv", "jsonl", "json"] = "csv"
    delimiter: str = Field(default=",", min_length=1, max_length=1)
    quotechar: str = Field(default='"', min_length=1, max_length=1)
    header: bool = True
    encoding: str = "utf-8"
    null_tokens: list[str] = Field(default_factory=lambda: [""])


class FileBinding(StrictModel):
    """Which members of a package hold this entity."""

    member: Regex = Field(description="Regex on the member path inside the package (or the file name for plain files)")
    format: FileFormat = Field(default_factory=FileFormat)
    extra_columns: Literal["reject", "ignore"] = Field(
        default="reject", description="Source columns not mapped in attributes"
    )


class Incremental(StrictModel):
    cursor_column: str = Field(min_length=1, description="Monotonic column, e.g. updated_at")
    initial_value: str | None = Field(default=None, description="Lower bound for the first sync (ISO date/time or number)")


class TableBinding(StrictModel):
    """Which database table holds this entity (rds sources)."""

    table: str = Field(pattern=r"^[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*)?$", description="schema.table")
    incremental: Incremental | None = Field(default=None, description="Omit for a full reload each sync")


class Attribute(StrictModel):
    """One STTM row: source column -> transforms -> target column, with checks."""

    name: Identifier = Field(description="Target column name")
    source: str | None = Field(default=None, description="Source column; defaults to name")
    type: FivetranType = "STRING"
    precision: int | None = Field(default=None, ge=1, le=38)
    scale: int | None = Field(default=None, ge=0, le=37)
    input_format: str | None = Field(default=None, description="strptime format for date/time input, e.g. %d%b%Y")
    nullable: bool = True
    description: str = ""
    business_rule: str = Field(default="", description="The rule in words (the STTM 'business rule' column)")
    transforms: list[AttributeTransform] = Field(default_factory=list)
    checks: list[AttributeCheck] = Field(default_factory=list)
    on_type_error: Literal["quarantine_row", "reject_package", "fail_sync"] = "quarantine_row"

    @model_validator(mode="after")
    def _decimal_args(self) -> "Attribute":
        if self.type == "DECIMAL" and (self.precision is None or self.scale is None):
            raise ValueError("DECIMAL needs precision and scale")
        if self.type != "DECIMAL" and (self.precision is not None or self.scale is not None):
            raise ValueError("precision/scale apply to DECIMAL only")
        if self.scale is not None and self.precision is not None and self.scale > self.precision:
            raise ValueError("scale cannot exceed precision")
        return self

    @property
    def source_column(self) -> str:
        return self.source or self.name


class Entity(StrictModel):
    name: Identifier = Field(description="Target table name")
    description: str = ""
    file: FileBinding | None = None
    table: TableBinding | None = None
    load: Literal["upsert", "append"] = Field(
        default="upsert", description="upsert needs primary_key; append keys rows by package, member and row number"
    )
    primary_key: list[Identifier] = Field(default_factory=list)
    attributes: list[Attribute] = Field(min_length=1)
    row_checks: list[RowCheck] = Field(default_factory=list, description="Cross-attribute checks over `row`")
    checks: list[EntityCheck] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistency(self) -> "Entity":
        if (self.file is None) == (self.table is None):
            raise ValueError("set exactly one of file (sftp/ftp) or table (rds)")
        names = [a.name for a in self.attributes]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate attribute names: {sorted(dupes)}")
        reserved = {n for n in names if n.startswith("pf_") or n.startswith("_fivetran")}
        if reserved:
            raise ValueError(f"attribute names {sorted(reserved)} are reserved for lineage/system columns")
        if self.load == "upsert" and not self.primary_key:
            raise ValueError("load: upsert needs primary_key")
        missing = [k for k in self.primary_key if k not in names]
        if missing:
            raise ValueError(f"primary_key columns {missing} are not attributes")
        nullable_keys = [a.name for a in self.attributes if a.name in self.primary_key and a.nullable]
        if nullable_keys:
            raise ValueError(f"primary_key columns {nullable_keys} must be nullable: false")
        return self


class SttmFile(StrictModel):
    """sttm.yaml"""

    entities: list[Entity] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> "SttmFile":
        names = [e.name for e in self.entities]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate entity names: {sorted(dupes)}")
        reserved = {n for n in names if n.startswith("pf_")}
        if reserved:
            raise ValueError(f"entity names {sorted(reserved)} are reserved for framework evidence tables")
        return self
