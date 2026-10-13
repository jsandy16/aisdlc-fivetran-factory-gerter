"""Helpers for the factory_spec tests: a private copy of the worked example repository and spec-file edits.

Every test works on synthetic data only (the worked example's accounts, hosts and studies are made up).
A uniquely named module (not conftest) so test files can import it under pytest's default import mode.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from factory_core import yamlio

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_REPO = REPO_ROOT / "examples" / "team-pipelines"
PLATFORM_SETTINGS = REPO_ROOT / "platform" / "platform-settings"
PIPELINE = "study_pkg_delivery"


def copy_example(dest: Path, *, platform_settings: Path | None = PLATFORM_SETTINGS, team: str = "clinops_data") -> Path:
    """Copy the example team repository to dest and point factory.toml at the platform settings by absolute path."""
    shutil.copytree(EXAMPLE_REPO, dest, ignore=shutil.ignore_patterns("__pycache__"))
    lines = ['api_version = "factory/v1"', f'team = "{team}"', 'environments = ["dev", "test", "ppt", "prod"]']
    if platform_settings is not None:
        lines.append(f'platform_settings = "{platform_settings.as_posix()}"')
    (dest / "factory.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def edit(pipeline_dir: Path, fname: str, mutate: Callable[[Any], None]) -> None:
    """Load a spec file round-trip, apply mutate(doc) in place and write it back."""
    f = pipeline_dir / "spec" / fname
    doc = yamlio.load_path(f)
    mutate(doc)
    yamlio.dump_path(doc, f)


def item(items: Any, value: str, key: str = "id") -> Any:
    """The list item whose key (rule_id, task_id, id) equals value."""
    for it in items:
        if it.get(key) == value:
            return it
    raise KeyError(value)


def rule(doc: Any, rule_id: str) -> Any:
    return item(doc["rules"], rule_id, "rule_id")


def task(doc: Any, task_id: str) -> Any:
    return item(doc["orchestration"]["tasks"], task_id, "task_id")
