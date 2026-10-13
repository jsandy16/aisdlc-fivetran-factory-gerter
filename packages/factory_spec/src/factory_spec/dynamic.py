"""Deterministic Pydantic models from field specs (portal responses, SQS messages, manual run conf).

Fields are created in sorted order with internal names f0, f1, … and the original name as alias, so no field name can
shadow a BaseModel attribute (e.g. `schema`, `json`). Model names are derived from the path, never random.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from .sections.fields import FieldSpec, RecordSpec


def _camel(*parts: str) -> str:
    return "".join(p[:1].upper() + p[1:] for part in parts for p in part.split("_") if p)


def _annotation(name: str, spec: FieldSpec, unknown: str) -> Any:
    if spec.type == "string":
        return str
    if spec.type == "integer":
        return int
    if spec.type == "boolean":
        return bool
    if spec.type == "enum":
        return Literal[tuple(spec.values or ())]
    if spec.type == "list":
        if spec.items is None:
            raise ValueError(f"list field '{name}' has no items")
        return list[_annotation(f"{name}_item", spec.items, unknown)]  # type: ignore[misc]
    if spec.fields is None:
        raise ValueError(f"object field '{name}' has no fields")
    return build_model(name, spec.fields, unknown_fields=unknown)


def _field_info(spec: FieldSpec, alias: str) -> Any:
    kw: dict[str, Any] = {"alias": alias}
    if spec.pattern is not None:
        kw["pattern"] = spec.pattern
    if spec.max_length is not None:
        kw["max_length"] = spec.max_length
    if spec.minimum is not None:
        kw["ge"] = spec.minimum
    if spec.maximum is not None:
        kw["le"] = spec.maximum
    if spec.max_items is not None:
        kw["max_length"] = spec.max_items
    if not spec.required:
        kw["default"] = None
    return Field(**kw)


def build_model(name: str, fields: dict[str, FieldSpec], *, unknown_fields: str = "ignore") -> type[BaseModel]:
    config = ConfigDict(
        strict=True, frozen=True, extra="forbid" if unknown_fields == "reject" else "ignore", populate_by_name=False
    )
    defs: dict[str, Any] = {}
    for i, fname in enumerate(sorted(fields)):
        spec = fields[fname]
        ann = _annotation(_camel(name, fname), spec, unknown_fields)
        if not spec.required:
            ann = ann | None
        defs[f"f{i}"] = (ann, _field_info(spec, fname))
    return create_model(_camel(name), __config__=config, **defs)


def record_model(name: str, record: RecordSpec) -> type[BaseModel]:
    return build_model(name, record.fields, unknown_fields=record.unknown_fields)
