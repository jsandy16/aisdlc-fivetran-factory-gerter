"""Exported JSON Schemas: committed copies are current, byte-stable across processes, and accept the worked example."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pydantic
import pytest
from factory_spec.files import SPEC_FILES
from factory_spec.schema_export import INDEX, check_schemas, committed_dir, export_schemas
from spec_helpers import EXAMPLE_REPO, PIPELINE, PLATFORM_SETTINGS

from factory_core import yamlio

SPEC_DIR = EXAMPLE_REPO / "pipelines" / PIPELINE / "spec"


def _schema(name: str) -> dict[str, Any]:
    return json.loads(export_schemas()[name])  # type: ignore[no-any-return]


def test_committed_schemas_are_current() -> None:
    assert check_schemas(committed_dir()) == []
    index = json.loads((committed_dir() / INDEX).read_text(encoding="utf-8"))
    assert index["pydantic"] == pydantic.VERSION
    assert sorted(index["files"]) == sorted(n for n in export_schemas() if n != INDEX)


def test_schemas_identical_across_processes() -> None:
    code = (
        "import json, sys; from factory_spec.schema_export import export_schemas; "
        "sys.stdout.write(json.dumps(export_schemas(), sort_keys=True))"
    )
    outs = [
        subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("0", "1")
    ]
    assert outs[0] == outs[1]


@pytest.mark.parametrize("fname", sorted(SPEC_FILES))
def test_example_validates_against_exported_schema(fname: str) -> None:
    schema = _schema(fname.removesuffix(".yaml") + ".schema.json")
    doc = yamlio.to_plain(yamlio.load_path(SPEC_DIR / fname))
    jsonschema.Draft202012Validator(schema).validate(doc)


@pytest.mark.parametrize("env", ["dev", "test", "ppt", "prod"])
def test_platform_settings_validate_against_schema(env: str) -> None:
    schema = _schema("platform-settings.schema.json")
    jsonschema.Draft202012Validator(schema).validate(json.loads((PLATFORM_SETTINGS / f"{env}.json").read_text()))


def test_schema_rejects_unknown_fields_and_bad_ops() -> None:
    v = jsonschema.Draft202012Validator(_schema("pipeline.schema.json"))
    doc = yamlio.to_plain(yamlio.load_path(SPEC_DIR / "pipeline.yaml"))
    doc["metadata"]["nmae"] = "typo"
    assert not v.is_valid(doc)
    rules = jsonschema.Draft202012Validator(_schema("rules.schema.json"))
    doc = yamlio.to_plain(yamlio.load_path(SPEC_DIR / "rules.yaml"))
    doc["rules"][0]["implementation"]["op"] = "package.rar"
    assert not rules.is_valid(doc)


def test_schema_annotations_for_editors_and_the_ba() -> None:
    pipe = _schema("pipeline.schema.json")
    assert pipe["x-owner-role"] == "data_engineer"
    assert pipe["x-hash"] == "logic"
    assert _schema("destinations.schema.json")["x-owner-role"] == "governance"
    assert _schema("environments.schema.json")["x-hash"] == "binding"
    md = pipe["$defs"]["Metadata"]["properties"]
    assert all("x-ba-label" in p for p in md.values())
    text = export_schemas()["rules.schema.json"]
    assert '"x-status": "planned"' in text  # planned ops are visible to editors but marked
    assert '"title"' not in text


def test_files_registry_is_complete() -> None:
    assert sorted(SPEC_FILES) == [
        "destinations.yaml",
        "environments.yaml",
        "integrations.yaml",
        "pipeline.yaml",
        "rules.yaml",
        "security.yaml",
    ]
    owners = {f: s.owner_role for f, s in SPEC_FILES.items()}
    assert owners["rules.yaml"] == "business_owner"
    assert owners["security.yaml"] == owners["destinations.yaml"] == "governance"
    assert owners["environments.yaml"] == "platform"
    assert {s.hash_kind for f, s in SPEC_FILES.items() if f in ("environments.yaml", "destinations.yaml")} == {
        "binding"
    }


def test_committed_dir_is_package_data() -> None:
    assert Path(committed_dir()).name == "schemas"
    assert (Path(committed_dir()) / "pipeline.schema.json").is_file()
