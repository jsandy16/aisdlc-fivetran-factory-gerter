"""Resource catalog (catalog/resources.v1.yaml): AWS resources a pipeline gets by configuration, rendered generically.

Pure logic shared by the validator (`factory validate` checks every instance) and the Terraform generator. A type's
parameters become a pydantic model at load time, so a wrong or missing parameter is a validation error with a message
a person can act on, before anything is generated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cache
from importlib import resources as ir
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from factory_core import yamlio

_TOKEN = re.compile(r"<([a-z_]+)(?::([A-Za-z0-9_]+))?(?:\.([A-Za-z0-9_.]+))?>")
_KEBAB = re.compile(r"[^a-z0-9-]")
PY = {"string": str, "integer": int, "number": float, "boolean": bool, "list": list[Any], "object": dict[str, Any]}


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["string", "integer", "number", "boolean", "list", "object", "ref"]
    required: bool = False
    default: Any = None
    enum: list[Any] | None = None
    min: float | None = None
    max_length: int | None = None
    ref_types: list[str] | None = None


class ResourceTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: str = Field(pattern=r"^aws_[a-z0-9_]+$")
    body: dict[str, Any]
    when: str | None = Field(default=None, pattern=r"^params\.[a-z_]+$")
    lookup: str | None = None
    import_id: str | None = None


class ResourceType(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    summary: str
    params: dict[str, ParamSpec] = {}
    resources: dict[str, ResourceTemplate]
    outputs: dict[str, str] = {}

    def params_model(self, name: str) -> type[BaseModel]:
        fields: dict[str, Any] = {}
        for p, s in self.params.items():
            py: Any = str if s.type == "ref" else PY[s.type]
            kw: dict[str, Any] = {}
            if s.enum is not None:
                py = Literal[tuple(s.enum)]
            if s.min is not None:
                kw["ge"] = s.min
            if s.max_length is not None:
                kw["max_length"] = s.max_length
            fields[p] = (py, Field(..., **kw)) if s.required else (py | None, Field(s.default, **kw))
        return create_model(f"Params_{name}", __config__=ConfigDict(extra="forbid", strict=False), **fields)


class ResourceCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    types: dict[str, ResourceType]


@cache
def resource_catalog() -> ResourceCatalog:
    text = ir.files("factory_spec.catalog").joinpath("resources.v1.yaml").read_text(encoding="utf-8")
    return ResourceCatalog.model_validate(yamlio.to_plain(yamlio.load_text(text)))


@dataclass
class Instance:
    id: str
    type: str
    params: dict[str, Any]
    name: str | None = None
    group: str = "resources"
    force_existing: bool = False


@dataclass
class Rendered:
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)  # tf type -> key -> body
    groups: dict[tuple[str, str], str] = field(default_factory=dict)  # (tf type, key) -> ownership group
    lookups: dict[str, str] = field(default_factory=dict)  # tf type -> lookup field
    import_ids: dict[str, str] = field(default_factory=dict)  # address -> import id
    forced: set[tuple[str, str]] = field(default_factory=set)  # always looked up (the binding names an existing one)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)  # instance id -> outputs


def check_instances(instances: list[Instance], catalog: ResourceCatalog | None = None) -> list[str]:
    """Plain-English problems with the instances (unknown type, bad parameter, a reference to nothing)."""
    cat = catalog or resource_catalog()
    by_id = {i.id: i for i in instances}
    out: list[str] = []
    for inst in instances:
        rtype = cat.types.get(inst.type)
        if rtype is None:
            out.append(f"{inst.id}: unknown resource type '{inst.type}' (known: {', '.join(sorted(cat.types))})")
            continue
        try:
            rtype.params_model(inst.type).model_validate(inst.params)
        except ValidationError as exc:
            out += [f"{inst.id}: parameter {'.'.join(str(x) for x in e['loc'])}: {e['msg']}"
                    for e in exc.errors(include_input=False)]  # fmt: skip
            continue
        for p, s in rtype.params.items():
            target = inst.params.get(p)
            if s.type == "ref" and target is not None:
                other = by_id.get(str(target))
                if other is None:
                    out.append(f"{inst.id}: {p} names '{target}', which is not a resource of this pipeline")
                elif s.ref_types and other.type not in s.ref_types:
                    out.append(f"{inst.id}: {p} must be a {' or '.join(s.ref_types)}, not a {other.type}")
    return out


def default_name(team: str, pipeline_id: str, instance_id: str) -> str:
    name = _KEBAB.sub("-", f"factory-{team}-{pipeline_id}-{instance_id}".lower())
    return name if len(name) <= 64 else name[:55] + "-" + __import__("hashlib").sha256(name.encode()).hexdigest()[:8]


def render(instances: list[Instance], facts: dict[str, str], catalog: ResourceCatalog | None = None) -> Rendered:
    """facts: env, team, pipeline_id, account, region, partition. Instances must pass check_instances first."""
    cat = catalog or resource_catalog()
    out = Rendered()
    by_id = {i.id: i for i in instances}
    full: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    for inst in instances:
        rtype = cat.types[inst.type]
        full[inst.id] = rtype.params_model(inst.type).model_validate(inst.params).model_dump()
        raw = inst.name or default_name(facts["team"], facts["pipeline_id"], inst.id)
        names[inst.id] = raw.replace("<env>", facts["env"])

    def tf_key(inst: Instance, rkey: str) -> str:
        return inst.id if len(cat.types[inst.type].resources) == 1 else f"{inst.id}_{rkey}"

    def outputs_of(iid: str) -> dict[str, Any]:
        if iid not in out.outputs:
            inst = by_id[iid]
            out.outputs[iid] = {k: subst(v, inst) for k, v in cat.types[inst.type].outputs.items()}
        return out.outputs[iid]

    missing = object()

    def token(inst: Instance, kind: str, arg: str | None, rest: str | None) -> Any:
        params = full[inst.id]
        if kind == "params":
            return params.get(str(rest), None) if rest else missing
        if kind == "json":
            v = params.get(str(rest).removeprefix("params.")) if arg == "params" else None
            return None if v is None else json.dumps(v, sort_keys=True)
        if kind == "out":
            ref = params.get(str(arg))
            return None if ref is None else outputs_of(str(ref)).get(str(rest))
        if kind == "tf":
            return f"{cat.types[inst.type].resources[str(arg)].type}.{tf_key(inst, str(arg))}"
        if kind == "name":
            return names[inst.id]
        if kind == "id":
            return inst.id
        return facts.get(kind, missing)

    def subst(value: Any, inst: Instance) -> Any:
        if isinstance(value, dict):
            return {k: v2 for k, v in value.items() if (v2 := subst(v, inst)) is not None}
        if isinstance(value, list):
            return [v2 for v in value if (v2 := subst(v, inst)) is not None]
        if not isinstance(value, str):
            return value
        whole = _TOKEN.fullmatch(value)
        if whole:
            v = token(inst, *whole.groups())
            return value if v is missing else v

        def one(m: re.Match[str]) -> str:
            v = token(inst, *m.groups())
            return m.group(0) if v is missing or v is None else str(v)

        return _TOKEN.sub(one, value)

    for inst in instances:
        rtype = cat.types[inst.type]
        for rkey, tmpl in rtype.resources.items():
            if tmpl.when and full[inst.id].get(tmpl.when.removeprefix("params.")) is None:
                continue
            key = tf_key(inst, rkey)
            out.resources.setdefault(tmpl.type, {})[key] = subst(tmpl.body, inst)
            out.groups[(tmpl.type, key)] = inst.group
            if tmpl.lookup:
                out.lookups[tmpl.type] = tmpl.lookup
            if tmpl.import_id:
                out.import_ids[f"{tmpl.type}.{key}"] = str(subst(tmpl.import_id, inst))
            if inst.force_existing:
                out.forced.add((tmpl.type, key))
        outputs_of(inst.id)
    return out
