"""LLM boundary: strict output schemas, replay determinism, skill files."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pf.llm.base import strict_schema
from pf.llm.schemas import CompiledRule, Explanation, RequirementProposal

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"


def _walk(node, path=""):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}/{i}")


@pytest.mark.parametrize("model", [CompiledRule, Explanation, RequirementProposal])
def test_strict_schema_is_self_consistent(model):
    s = strict_schema(model)
    text = json.dumps(s)
    assert "$ref" not in text and "$defs" not in text  # inlined
    for path, node in _walk(s):
        if "properties" in node:
            assert node["additionalProperties"] is False, path
            assert set(node["required"]) == set(node["properties"]), path  # regression: a field named `pattern`


def test_fields_named_like_keywords_survive():
    s = strict_schema(RequirementProposal)
    check = s["properties"]["entities"]["items"]["properties"]["attributes"]["items"]["properties"]["checks"]["items"]
    assert {"pattern", "min", "max"} <= set(check["properties"])


def test_replay_is_deterministic_and_offline(onc):
    from pf.bundle import load_bundle
    from pf.rulecompiler import compile_rules

    (onc / "rules.lock.yaml").unlink()
    data1, _ = compile_rules(load_bundle(onc))
    a = (onc / "rules.lock.yaml").read_text()
    (onc / "rules.lock.yaml").unlink()
    data2, _ = compile_rules(load_bundle(onc))
    assert a == (onc / "rules.lock.yaml").read_text()
    assert data1.rules["visit_before_receipt"].verification == "passed"


def test_replay_miss_is_an_error(onc, monkeypatch):
    from pf.bundle import load_bundle
    from pf.envelope import PfError
    from pf.rulecompiler import compile_rules

    monkeypatch.setenv("PF_LLM_FIXTURES", str(onc / "nowhere"))
    (onc / "rules.lock.yaml").unlink()
    with pytest.raises(PfError):
        compile_rules(load_bundle(onc))


@pytest.mark.parametrize("skill", sorted(p.parent.name for p in PLUGIN.glob("skills/*/SKILL.md")))
def test_skill_files_follow_the_spec(skill):
    text = (PLUGIN / "skills" / skill / "SKILL.md").read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "frontmatter"
    fm = dict(line.split(": ", 1) for line in m.group(1).splitlines() if ": " in line)
    assert fm["name"] == skill and re.fullmatch(r"[a-z0-9-]{1,64}", skill)
    assert "claude" not in skill and "anthropic" not in skill
    assert 0 < len(fm["description"]) <= 1024 and "<" not in fm["description"]
    assert len(text.splitlines()) < 500


def test_plugin_manifest_and_hooks():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "pipeline-factory"
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    assert {"PreToolUse", "PostToolUse"} <= set(hooks["hooks"])
