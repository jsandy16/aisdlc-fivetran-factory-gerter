"""Custom rules through the plugin API (ADR-0020): hash pinning, the import allow-list at load, the declared side
effects, the output contract and on_failure. The allow-list is a lint, not a sandbox; these tests prove the lint and
the contract hold, not that hostile code is contained."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from factory_core.errors import ConfigError, SecurityError
from factory_runtime.ops import CheckFailed
from factory_runtime.plugins import load_plugin, plugin_contract
from rt_helpers import UNIT_REPO, LogCapture, OpHarness, copy_repo, make_config, publish_bundle, run_task, setup_stores

from factory_core import yamlio

CONF = {"request_id": "REQ-00000001", "study_id": "ONC101"}
HEADER = (
    "from __future__ import annotations\n\n"
    "from factory_runtime.plugin_api import CustomRule, PluginContext, PluginInputs, PluginOutputs, PluginParams\n"
    "from factory_runtime.plugin_api import RuleFailed\n\n\n"
    "class Params(PluginParams):\n    pass\n\n\n"
    "class Inputs(PluginInputs):\n    file_name: str\n\n\n"
    "class Outputs(PluginOutputs):\n    label: str\n\n\n"
)


def _rule(body: str, side_effects: str = "frozenset()") -> str:
    return (
        HEADER + "class Rule(CustomRule[Params, Inputs, Outputs]):\n"
        f"    side_effects = {side_effects}\n\n"
        "    def run(self, ctx: PluginContext, params: Params, inputs: Inputs) -> Outputs:\n" + body
    )


@pytest.fixture
def unit(tmp_path: Path, workdir: Path, aws: None) -> dict[str, Any]:
    repo = copy_repo(UNIT_REPO, tmp_path / "repo", environments=["dev"])
    cfg = make_config(workdir, "unit_custom")
    setup_stores(cfg)
    return {"dir": repo / "pipelines" / "unit_custom", "cfg": cfg}


def _install(pdir: Path, source: str, *, update_hash: bool = True) -> str:
    impl = pdir / "rules" / "r_label" / "impl.py"
    impl.write_text(source)
    sha = hashlib.sha256(source.encode()).hexdigest()
    if update_hash:
        f = pdir / "spec" / "rules.yaml"
        doc = yamlio.load_path(f)
        doc["rules"][1]["implementation"]["code_sha256"] = sha
        yamlio.dump_path(doc, f)
    return sha


def _harness(unit: dict[str, Any]) -> OpHarness:
    h = OpHarness(unit["dir"], unit["cfg"], trigger_body=CONF)
    h.run_rule("r_name")
    return h


def test_custom_rule_runs_through_the_plugin_api(unit: dict[str, Any]) -> None:
    with LogCapture() as logs:
        h = _harness(unit)
        assert h.run_rule("r_label") == ["ONC101:0001:15"]
    assert (h.ctx("r_label").workdir / "label.json").read_bytes() == b'{"label": "ONC101:0001:15", "at": 1791000000}'
    assert [e["length"] for e in logs.events("plugin_labelled")] == [15]
    code = (unit["dir"] / "rules" / "r_label" / "impl.py").read_bytes()
    plugin = load_plugin(unit["dir"], "r_label", hashlib.sha256(code).hexdigest())
    assert plugin_contract(plugin) == {
        "inputs": {"file_name": "str"},
        "outputs": {"label": "str"},
        "side_effects": ["writes_workdir"],
        "aws_actions": [],
    }


def test_changed_code_is_refused_with_exit_77(unit: dict[str, Any]) -> None:
    cfg = unit["cfg"]
    uri, sha = publish_bundle(cfg, unit["dir"])
    assert run_task(cfg, uri, sha, "t1_label", "REQ-00000001", conf=CONF) == 0
    # the code is changed on a warm worker, inside the extracted bundle the runtime caches by bundle hash
    cached = cfg.workdir / "bundles" / sha / "rules" / "r_label" / "impl.py"
    cached.write_text(cached.read_text().replace("len(inputs.file_name)}", "0}"))
    assert run_task(cfg, uri, sha, "t1_label", "REQ-00000002", conf={**CONF, "request_id": "REQ-00000002"}) == 77
    # and in the source tree, before any bundle is built
    impl = unit["dir"] / "rules" / "r_label" / "impl.py"
    impl.write_text(impl.read_text() + "\n# changed after review\n")
    with pytest.raises(SecurityError) as ei:
        _harness(unit).run_rule("r_label")
    assert ei.value.code == "custom_rule_code_mismatch"


@pytest.mark.parametrize(
    "line",
    [
        "import os",
        "import subprocess",
        "from boto3 import client",
        "import socket as s",
        "from factory_runtime import executor",
        "from . import sibling",
        "x = __import__('os')",
        "x = getattr(object, 'mro')",
        "x = ().__class__.__bases__",
        "x = eval('1')",
        "x = open('/etc/passwd')",
        "x = '__globals__'",
    ],
)
def test_forbidden_code_is_refused_at_load(tmp_path: Path, line: str) -> None:
    """A bundle built from an unchecked tree: the runtime checks the code again before executing anything."""
    source = _rule("        return Outputs(label='x')\n").replace("class Params", f"{line}\n\n\nclass Params", 1)
    rules = tmp_path / "rules" / "r_bad"
    rules.mkdir(parents=True)
    (rules / "impl.py").write_text(source)
    with pytest.raises(SecurityError) as ei:
        load_plugin(tmp_path, "r_bad", hashlib.sha256(source.encode()).hexdigest())
    assert ei.value.code == "custom_rule_import_forbidden"


def test_undeclared_side_effect_is_security(unit: dict[str, Any]) -> None:
    _install(unit["dir"], _rule("        ctx.write_file('x.txt', b'x')\n        return Outputs(label='x')\n"))
    with pytest.raises(SecurityError) as ei:
        _harness(unit).run_rule("r_label")
    assert ei.value.code == "custom_rule_side_effect_undeclared"


def test_rule_failed_follows_on_failure(unit: dict[str, Any]) -> None:
    _install(unit["dir"], _rule("        raise RuleFailed('the label would be ambiguous')\n"))
    with pytest.raises(CheckFailed) as ei:
        _harness(unit).run_rule("r_label")
    assert ei.value.code == "custom_rule_failed"
    from factory_spec.load import load_spec
    from factory_spec.lock import write_lock

    spec = load_spec(unit["dir"]).spec
    assert spec is not None
    write_lock(spec)
    uri, sha = publish_bundle(unit["cfg"], unit["dir"])
    assert run_task(unit["cfg"], uri, sha, "t1_label", "REQ-00000001", conf=CONF) == 65  # on_failure: data


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("        return {'label': 'x'}\n", "custom_rule_output_invalid"),  # not the Outputs model
        ("        raise ValueError('a bug')\n", None),  # an unexpected exception is internal (70)
    ],
)
def test_output_contract_and_bugs(unit: dict[str, Any], body: str, code: str | None) -> None:
    _install(unit["dir"], _rule(body))
    if code is None:
        with pytest.raises(ValueError, match="a bug"):
            _harness(unit).run_rule("r_label")
        return
    with pytest.raises(ConfigError) as ei:
        _harness(unit).run_rule("r_label")
    assert ei.value.code == code


def test_contract_must_be_a_typed_rule(tmp_path: Path) -> None:
    source = HEADER + "class Rule:\n    pass\n"
    d = tmp_path / "rules" / "r_x"
    d.mkdir(parents=True)
    (d / "impl.py").write_text(source)
    with pytest.raises(ConfigError) as ei:
        load_plugin(tmp_path, "r_x", hashlib.sha256(source.encode()).hexdigest())
    assert ei.value.code == "custom_rule_contract_invalid"
    bad = _rule("        return Outputs(label='x')\n", side_effects="frozenset({'opens_sockets'})")
    (d / "impl.py").write_text(bad)
    with pytest.raises(ConfigError) as ei:
        load_plugin(tmp_path, "r_x", hashlib.sha256(bad.encode()).hexdigest())
    assert ei.value.code == "custom_rule_contract_invalid"


def test_every_available_op_is_implemented() -> None:
    """The runtime implements exactly the catalog's available ops (plus the expression evaluator)."""
    from factory_runtime.ops import load_all
    from factory_spec.catalog import op_catalog

    available = {name for name, c in op_catalog().items() if c.status == "available"}
    assert set(load_all()) - {"expression"} == available
