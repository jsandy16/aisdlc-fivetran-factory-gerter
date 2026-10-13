"""The capability catalog, derived from the pydantic models (never hand-maintained).

`pf catalog --json` gives agents and people the exact list of source types, destination types,
checks and transforms with their parameters, so skills never hard-code lists.
"""
from __future__ import annotations

from typing import Any, get_args

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from .specs import quality as q
from .specs import rules as r
from .specs import source as s

CATEGORIES: dict[str, tuple[str, Any]] = {
    "package_checks": ("source.yaml: source.package.checks — structural checks on each delivery (file sources)", r.PackageCheck),
    "package_transforms": ("source.yaml: source.package.transforms — unpacking and member renames", r.PackageTransform),
    "entity_checks": ("sttm.yaml: entities[].checks — whole-entity checks before anything is emitted", r.EntityCheck),
    "row_checks": ("sttm.yaml: entities[].row_checks — cross-attribute checks over `row`", r.RowCheck),
    "attribute_checks": ("sttm.yaml: entities[].attributes[].checks", r.AttributeCheck),
    "attribute_transforms": ("sttm.yaml: entities[].attributes[].transforms — applied in order", r.AttributeTransform),
    "lake_checks": ("quality.yaml: checks — whole-table checks after each sync (Athena)", q.LakeCheck),
}
SOURCE_TYPES = {"sftp": s.SftpSource, "ftp": s.FtpSource, "rds": s.RdsSource}
DESTINATION_TYPES = {"s3": "Fivetran Managed Data Lake on Amazon S3 (Iceberg, optional Glue)"}
_SKIP = {"kind", "id", "description", "mode", "tags", "on_fail"}


def _members(union: Any) -> list[type[BaseModel]]:
    inner = get_args(union)[0]  # Annotated[Union[...], Field]
    args = get_args(inner)
    return list(args) if args else [inner]


def _kind(model: type[BaseModel]) -> str:
    return get_args(model.model_fields["kind"].annotation)[0]


def _type_str(f: FieldInfo) -> str:
    t = f.annotation
    s_ = str(t).replace("typing.", "").replace("pf.specs.common.", "").replace("NoneType", "None")
    return s_.replace("<class '", "").replace("'>", "")


def describe_model(model: type[BaseModel], skip: set[str] = _SKIP) -> dict[str, Any]:
    params = {}
    for name, f in model.model_fields.items():
        if name in skip:
            continue
        params[name] = {
            "type": _type_str(f),
            "required": f.is_required(),
            "default": None if f.is_required() else _jsonable(f.get_default(call_default_factory=True)),
            "description": f.description or "",
        }
    doc = (model.__doc__ or "").strip().splitlines()
    on_fail = model.model_fields.get("on_fail")
    return {
        "doc": doc[0] if doc else "",
        "params": params,
        "on_fail": {
            "default": on_fail.get_default() if on_fail else None,
            "allowed": list(get_args(on_fail.annotation)) if on_fail else [],
        },
    }


def _jsonable(v: Any) -> Any:
    if isinstance(v, BaseModel):
        return v.model_dump(mode="json")
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def catalog() -> dict[str, Any]:
    out: dict[str, Any] = {
        "source_types": {k: describe_model(m, {"type", "package"}) for k, m in SOURCE_TYPES.items()},
        "destination_types": DESTINATION_TYPES,
        "custom_rule_forms": {
            "expression": "CEL expression; `row` = all attributes, `value` = this attribute, `package` = delivery facts",
            "python": "module:function in the pipeline's rules/ folder; reviewed code, returns failure reasons",
            "english": "plain-English intent + >=3 owner examples; compiled to CEL by `pf rules compile`, accepted by a person",
        },
    }
    for cat, (where, union) in CATEGORIES.items():
        out[cat] = {"where": where, "kinds": {_kind(m): describe_model(m) for m in _members(union)}}
    return out


def kind_names(category: str) -> list[str]:
    return [_kind(m) for m in _members(CATEGORIES[category][1])]


def comment_block(category: str) -> str:
    """One-line-per-kind summary used as a comment in scaffolded YAML."""
    lines = [f"Available {category.replace('_', ' ')}:"]
    for m in _members(CATEGORIES[category][1]):
        params = ", ".join(
            f"{n}{'' if f.is_required() else '?'}" for n, f in m.model_fields.items() if n not in _SKIP
        )
        lines.append(f"  {_kind(m)}({params})")
    return "\n".join(lines)

