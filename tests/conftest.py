"""Shared fixtures: a throw-away pipeline-factory repository with the oncology_labs example copied in."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

FRAMEWORK = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch):
    """Unit tests never call a model: LLM steps replay the recorded fixtures."""
    monkeypatch.setenv("PF_LLM_PROVIDER", "replay")
    monkeypatch.setenv("PF_LLM_FIXTURES", str(FRAMEWORK / "tests" / "llm-fixtures"))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("PF_AGENT_SESSION", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pf.toml").write_text("# pipeline-factory repository\n")
    shutil.copytree(FRAMEWORK / "examples" / "pipelines" / "oncology_labs", tmp_path / "pipelines" / "oncology_labs",
                    ignore=shutil.ignore_patterns("build", "intake"))
    old = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(old)


@pytest.fixture
def onc(repo: Path) -> Path:
    return repo / "pipelines" / "oncology_labs"
