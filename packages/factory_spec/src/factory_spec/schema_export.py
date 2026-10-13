"""Deterministic JSON Schema export of every spec file (for editor autocomplete), platform settings and factory.toml.

Byte-stable: no titles, sorted keys, LF; non-flat discriminator mappings are dropped (editors use the `const` of each
`oneOf` branch). Every property carries `x-ba-label` (a plain-English label) and each file root its `x-owner-role`.
`SCHEMAS.json` records the pydantic version and the sha256 of each file so drift is detected in CI.
"""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from pathlib import Path
from typing import Any

import pydantic
from pydantic.json_schema import GenerateJsonSchema

from .files import SPEC_FILES
from .platform import PlatformSettings
from .repo import RepoConfig

SCHEMA_ID = "urn:pipeline-factory:spec:v1:{name}"
INDEX = "SCHEMAS.json"


class FactoryJsonSchema(GenerateJsonSchema):
    def field_title_should_be_set(self, schema: Any) -> bool:
        return False


def _label(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def _post(node: Any, *, keys_are_names: bool = False) -> Any:
    """Walk schema nodes. In `properties` / `$defs` maps the keys are names, not keywords, and are kept."""
    if isinstance(node, list):
        return [_post(v) for v in node]
    if not isinstance(node, dict):
        return node
    if keys_are_names:
        return {k: _post(v) for k, v in node.items()}
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k == "title":
            continue
        out[k] = _post(v, keys_are_names=k in ("properties", "$defs", "patternProperties"))
    disc = out.get("discriminator")
    if isinstance(disc, dict) and any(not isinstance(v, str) for v in disc.get("mapping", {}).values()):
        out.pop("discriminator")
    props = out.get("properties")
    if isinstance(props, dict):
        for pname, prop in props.items():
            if isinstance(prop, dict) and "x-ba-label" not in prop:
                prop["x-ba-label"] = _label(pname)
    desc = out.get("description")
    if isinstance(desc, str) and desc.startswith("A planned op"):
        out["x-status"] = "planned"
    return out


def _schema(model: type[pydantic.BaseModel], name: str, extra: dict[str, Any]) -> str:
    s = model.model_json_schema(schema_generator=FactoryJsonSchema, mode="validation")
    s = _post(s)
    s["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    s["$id"] = SCHEMA_ID.format(name=name)
    s.update(extra)
    return json.dumps(s, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def export_schemas() -> dict[str, str]:
    out: dict[str, str] = {}
    for fname, sf in SPEC_FILES.items():
        stem = fname.removesuffix(".yaml")
        out[f"{stem}.schema.json"] = _schema(
            sf.model, stem, {"x-owner-role": sf.owner_role, "x-ba-label": sf.ba_label, "x-hash": sf.hash_kind}
        )
    out["platform-settings.schema.json"] = _schema(PlatformSettings, "platform-settings", {"x-owner-role": "platform"})
    out["factory-toml.schema.json"] = _schema(RepoConfig, "factory-toml", {"x-owner-role": "platform"})
    index = {
        "pydantic": pydantic.VERSION,
        "files": {k: hashlib.sha256(v.encode("utf-8")).hexdigest() for k, v in sorted(out.items())},
    }
    out[INDEX] = json.dumps(index, indent=2, sort_keys=True) + "\n"
    return out


def committed_dir() -> Path:
    return Path(str(resources.files("factory_spec").joinpath("schemas")))


def write_schemas(target: Path) -> list[str]:
    target.mkdir(parents=True, exist_ok=True)
    files = export_schemas()
    for name, text in files.items():
        (target / name).write_text(text, encoding="utf-8", newline="\n")
    return sorted(files)


def check_schemas(target: Path) -> list[str]:
    """Names of schema files that differ from (or are missing in) the target folder."""
    diffs = []
    for name, text in export_schemas().items():
        p = target / name
        if not p.is_file() or p.read_text(encoding="utf-8") != text:
            diffs.append(name)
    return diffs
