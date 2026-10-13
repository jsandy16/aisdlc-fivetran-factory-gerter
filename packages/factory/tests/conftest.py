from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cli_helpers import Run, copy_example
from factory.cli import main


@pytest.fixture
def example_repo(tmp_path: Path) -> Path:
    return copy_example(tmp_path / "repo")


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Run:
    """Run `factory <args>` in-process; with --json the output must be exactly one JSON envelope."""

    def run(*args: str) -> tuple[int, Any]:
        capsys.readouterr()
        code = main(list(args))
        out = capsys.readouterr().out
        if "--json" in args:
            return code, json.loads(out)
        return code, out

    return run
