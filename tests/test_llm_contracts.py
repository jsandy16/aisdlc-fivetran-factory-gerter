"""LLM boundary: strict output schemas, replay determinism, skill files."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pf.llm.base import strict_schema
from pf.llm.schemas import CompiledRule, Explanation, RequirementProposal

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"
STEPS = Path(__file__).resolve().parents[1] / "steps-plugin"


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


SKILL_FILES = sorted(p for root in (PLUGIN, STEPS) for p in root.glob("skills/*/SKILL.md"))


@pytest.mark.parametrize("path", SKILL_FILES, ids=[p.parent.name for p in SKILL_FILES])
def test_skill_files_follow_the_spec(path):
    text = path.read_text()
    skill = path.parent.name
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "frontmatter"
    fm = dict(line.split(": ", 1) for line in m.group(1).splitlines() if ": " in line)
    assert fm["name"] == skill and re.fullmatch(r"[a-z0-9-]{1,64}", skill)
    assert "claude" not in skill and "anthropic" not in skill
    assert 0 < len(fm["description"]) <= 1024 and "<" not in fm["description"]
    assert len(text.splitlines()) < 500


def test_headless_skills_are_not_in_the_interactive_plugin():
    assert sorted(p.parent.name for p in STEPS.glob("skills/*/SKILL.md")) == [
        "compiling-english-rules", "explaining-compiled-rules", "extracting-pipeline-requirements"]
    assert not any((PLUGIN / "skills" / n).exists() for n in ("compiling-english-rules", "extracting-pipeline-requirements"))
    for p in STEPS.glob("skills/*/SKILL.md"):
        assert "user-invocable: false" in p.read_text()


def test_plugin_manifest_and_hooks():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "pipeline-factory"
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    assert {"PreToolUse", "PostToolUse"} <= set(hooks["hooks"])


def test_acceptance_is_bound_to_content(onc):
    """Editing an accepted rule's expression or status in rules.lock.yaml invalidates the acceptance."""
    from pf.bundle import load_bundle
    from pf.lock import english_status

    text = (onc / "rules.lock.yaml").read_text()
    assert english_status(load_bundle(onc), __import__("pf.rulecompiler", fromlist=["x"]).load_rules_lock(onc)) == []
    tampered = text.replace("row.visit_date <= row.received_date", "true || row.visit_date <= row.received_date", 1)
    (onc / "rules.lock.yaml").write_text(tampered)
    from pf.rulecompiler import load_rules_lock

    codes = [i.code for i in english_status(load_bundle(onc), load_rules_lock(onc))]
    assert codes == ["acceptance_invalid"]


def test_wrong_operator_is_caught_by_mutation_testing(onc):
    """The model's own cases can agree with a wrong `<`; mutants make boundary examples mandatory."""
    from pf.bundle import load_bundle
    from pf.rulecompiler import Case, english_rules, evaluate, verify

    ref = next(r for r in english_rules(load_bundle(onc)) if r.rule_id == "visit_before_receipt")
    wrong = "row.visit_date != null && row.received_date != null && row.visit_date < row.received_date"
    cases = [Case(input={"visit_date": "2024-03-01", "received_date": d}, expected=e, source="generated",
                  actual="pass" if evaluate(ref, wrong, {"visit_date": "2024-03-01", "received_date": d}) else "fail")
             for d, e in (("2024-03-05", "pass"), ("2024-02-01", "fail"), ("2024-03-02", "pass"), ("2024-01-01", "fail"),
                          ("2024-04-01", "pass"), ("2023-01-01", "fail"))]
    problems = verify(ref, wrong, ["row.visit_date", "row.received_date"], cases)
    assert any("cannot tell the expression from" in p for p in problems)


def test_fixture_tampering_is_detected(onc, monkeypatch, tmp_path):
    import json as _j
    import shutil

    from pf.bundle import load_bundle
    from pf.envelope import PfError
    from pf.rulecompiler import compile_rules
    from tests.conftest import FRAMEWORK

    shutil.copytree(FRAMEWORK / "tests" / "llm-fixtures", tmp_path / "fx")
    f = next((tmp_path / "fx" / "explain_rule").glob("*.json"))
    rec = _j.loads(f.read_text())
    rec["output"]["explanation"] = "forged"
    f.write_text(_j.dumps(rec))
    monkeypatch.setenv("PF_LLM_FIXTURES", str(tmp_path / "fx"))
    (onc / "rules.lock.yaml").unlink()
    with pytest.raises(PfError) as e:
        compile_rules(load_bundle(onc))
    assert e.value.code == "fixture_tampered"
