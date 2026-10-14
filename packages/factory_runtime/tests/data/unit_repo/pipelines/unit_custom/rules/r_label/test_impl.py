"""Owner and edge cases of r_label, run through the plugin API (synthetic)."""

from __future__ import annotations

from pathlib import Path

import pytest
from factory_runtime.plugin_api import PluginContext, RuleFailed
from impl import Inputs, Params, Rule


def _ctx(tmp_path: Path) -> PluginContext:
    return PluginContext(run_date="2026-01-01", run_key_hash="0" * 12, now=0, workdir=tmp_path,
                         log=lambda *a, **k: None, opener=None)  # fmt: skip


def test_example(tmp_path: Path) -> None:
    assert Rule().run(_ctx(tmp_path), Params(), Inputs(file_name="ONC101_0001.txt")).label == "ONC101:0001:15"


def test_name_without_study_fails(tmp_path: Path) -> None:
    with pytest.raises(RuleFailed):
        Rule().run(_ctx(tmp_path), Params(), Inputs(file_name="nostudy.txt"))
