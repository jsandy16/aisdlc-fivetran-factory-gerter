from __future__ import annotations

from pathlib import Path

import pytest
from spec_helpers import PIPELINE, copy_example


@pytest.fixture
def example_repo(tmp_path: Path) -> Path:
    return copy_example(tmp_path / "repo")


@pytest.fixture
def pipeline_dir(example_repo: Path) -> Path:
    return example_repo / "pipelines" / PIPELINE
