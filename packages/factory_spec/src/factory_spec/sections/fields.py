"""Field specs: declare the shape of an SQS message, a portal response or manual run conf (no free-form JSON)."""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from ..common import StrictModel, ba
from ..fieldtypes import Identifier, Regex

FieldName = Identifier


class FieldSpec(StrictModel):
    type: Literal["string", "integer", "boolean", "enum", "list", "object"] = ba("Type", "Value type")
    required: bool = ba("Required", "The field must be present", default=True)
    pattern: Regex | None = ba("Pattern", "Regular expression a string must match", default=None)
    max_length: int | None = ba("Maximum length", "Maximum string length", default=None, ge=1)
    minimum: int | None = ba("Minimum", "Smallest allowed integer", default=None)
    maximum: int | None = ba("Maximum", "Largest allowed integer", default=None)
    values: list[str] | None = ba("Allowed values", "For enum fields", default=None)
    items: FieldSpec | None = ba("Item type", "For list fields", default=None)
    max_items: int | None = ba("Maximum items", "For list fields", default=None, ge=1)
    fields: dict[FieldName, FieldSpec] | None = ba("Fields", "For object fields", default=None)

    @model_validator(mode="after")
    def _shape(self) -> FieldSpec:
        if self.type == "enum" and not self.values:
            raise ValueError("an enum field needs 'values'")
        if self.type == "list" and self.items is None:
            raise ValueError("a list field needs 'items'")
        if self.type == "object" and not self.fields:
            raise ValueError("an object field needs 'fields'")
        if self.pattern is not None and self.type != "string":
            raise ValueError("'pattern' only applies to string fields")
        return self

    def depth(self) -> int:
        inner = [self.items.depth()] if self.items else []
        inner += [f.depth() for f in (self.fields or {}).values()]
        return 1 + max(inner, default=0)


class RecordSpec(StrictModel):
    unknown_fields: Literal["ignore", "reject"] = ba(
        "Unknown fields", "Ignore or reject fields that are not declared", default="ignore"
    )
    fields: dict[FieldName, FieldSpec] = ba("Fields", "Declared fields", min_length=1)

    @model_validator(mode="after")
    def _depth(self) -> RecordSpec:
        if any(f.depth() > 4 for f in self.fields.values()):
            raise ValueError("field specs may nest at most 4 levels deep")
        return self
