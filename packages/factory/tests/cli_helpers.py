"""Helpers for the factory CLI tests (a uniquely named module, importable under pytest's default import mode)."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from factory_core import yamlio

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_REPO = REPO_ROOT / "examples" / "team-pipelines"
PLATFORM_SETTINGS = REPO_ROOT / "platform" / "platform-settings"
FILLS = Path(__file__).parent / "starter_fills"
Run = Callable[..., tuple[int, Any]]  # the `cli` fixture: run(*args) -> (exit code, envelope or text)
ENVS = ("dev", "test", "ppt", "prod")


def copy_example(dest: Path) -> Path:
    shutil.copytree(EXAMPLE_REPO, dest, ignore=shutil.ignore_patterns("__pycache__"))
    (dest / "factory.toml").write_text(
        'api_version = "factory/v1"\nteam = "clinops_data"\nenvironments = ["dev", "test", "ppt", "prod"]\n'
        f'platform_settings = "{PLATFORM_SETTINGS.as_posix()}"\n',
        encoding="utf-8",
    )
    return dest


def fill_edits(starter: str) -> list[tuple[str, str, str]]:
    """(file, selector path, value as YAML text) for every placeholder of a starter, {env} expanded."""
    out: list[tuple[str, str, str]] = []
    for name in ("_common", starter):
        doc = yamlio.to_plain(yamlio.load_path(FILLS / f"{name}.yaml"))
        for fname, edits in doc.items():
            for path, value in edits.items():
                envs = ENVS if "{env}" in path else ("",)
                for env in envs:
                    v: Any = value.replace("{env}", env) if isinstance(value, str) else value
                    out.append((fname, path.replace("{env}", env), json.dumps(v)))
    return out
