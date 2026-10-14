"""Rule implementation → gates → review → a person's acceptance → orchestrated run and review package.

Synthetic recordings only (agents_helpers.Scripted). The code the agent "writes" is the unit_custom plugin.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from agents_helpers import Scripted, copy_unit_repo
from factory_agents.accept import accept_rule
from factory_agents.audit import AuditTrail
from factory_agents.errors import AgentError
from factory_agents.orchestrator import orchestrate
from factory_agents.rules import implement_rule
from factory_spec.validate import validate_dir


@pytest.fixture
def pdir(tmp_path: Path) -> Path:
    d = copy_unit_repo(tmp_path / "repo") / "pipelines" / "unit_custom"
    rule_dir = d / "rules" / "r_label"
    for f in ("review.json", "impl.lock.json"):
        (rule_dir / f).unlink()  # start from a rule nobody has reviewed or accepted
    return d


def _code(pdir: Path) -> tuple[str, str]:
    d = pdir / "rules" / "r_label"
    return (d / "impl.py").read_text(), (d / "test_impl.py").read_text()


def _impl(code: str, tests: str) -> dict[str, Any]:
    return {"builtin_op": None, "params_json": "{}", "code": code, "tests": tests, "notes": "custom code"}


EDGES = {"cases": [{"name": "no_study_prefix", "given_json": '{"file_name": "nostudy.txt"}',
                    "expect_json": '{"result": "fail"}', "why": "no underscore"},
                   {"name": "example", "given_json": "{}", "expect_json": "{}", "why": "shadows an owner case"}],
         "questions": []}  # fmt: skip
APPROVE = {
    "verdict": "approve",
    "findings": [],
    "summary": "Matches the plain English; no side effects beyond the workdir.",
}
REJECT = {"verdict": "reject", "summary": "Missing a check.",
          "findings": [{"severity": "major", "category": "correctness",
                        "message": "Unicode file names untested"}]}  # fmt: skip


def test_gate_failure_then_review_rejection_then_approval(pdir: Path, tmp_path: Path) -> None:
    code, tests = _code(pdir)
    bad = code.replace("import json\n", "import json\nimport os\n")
    unicode_test = (
        "\n\ndef test_unicode_name(tmp_path: Path) -> None:\n"
        '    assert Rule().run(_ctx(tmp_path), Params(), Inputs(file_name="ONC101_é.txt")).label == "ONC101:é:12"\n'
    )
    runner = Scripted(tmp_path / "rec", {
        "test_generation": [EDGES],
        "rule_implementation": [_impl(bad, tests), _impl(code, tests), _impl(code, tests + unicode_test)],
        "rule_review": [REJECT, APPROVE],
    })  # fmt: skip
    audit = AuditTrail(pdir)
    out = implement_rule(runner, pdir, "r_label", audit, tools=False)
    assert out.status == "awaiting_acceptance"
    assert out.escalation is None
    assert out.gates is not None
    assert out.gates.ok
    p2, p3 = runner.prompts["rule_implementation"][1:]
    assert "import of 'os' is not allowed" in p2  # the gate's finding was fed back
    assert "Unicode file names untested" in p3  # the review's finding was fed back
    edges = json.loads((pdir / "rules" / "r_label" / "edge_cases.json").read_text())
    assert [c["name"] for c in edges] == ["no_study_prefix"]  # an edge case never shadows an owner case
    review = json.loads((pdir / "rules" / "r_label" / "review.json").read_text())
    assert review["verdict"] == "approve"
    assert [(e["agent"], e["verdict"]) for e in audit.entries()] == [
        ("test_generation", "ok"),
        ("rule_implementation", "issues"),
        ("rule_implementation", "custom"),
        ("rule_review", "reject"),
        ("rule_implementation", "custom"),
        ("rule_review", "approve"),
    ]
    # not valid until a person accepts it
    assert any(i.code == "custom_rule_unaccepted" for i in validate_dir(pdir).errors)


def test_unknown_builtin_op_escalates(pdir: Path, tmp_path: Path) -> None:
    wrong = {"builtin_op": "label.everything", "params_json": "{}", "code": "", "tests": "", "notes": ""}
    runner = Scripted(tmp_path / "rec", {"test_generation": [EDGES], "rule_implementation": [wrong, wrong]})
    out = implement_rule(runner, pdir, "r_label", AuditTrail(pdir), tools=False)
    assert out.status == "escalated"
    assert out.escalation is not None
    assert out.escalation.last_issues == ["label.everything is not an available built-in op"]


def test_requirement_to_accepted_rule_and_review_package(pdir: Path, tmp_path: Path) -> None:
    code, tests = _code(pdir)
    requirement = "Purpose: label every study file with its study, sequence and name length for the archive index."
    intake_answer = {"patches": [{"file": "pipeline.yaml", "op": "set", "path": "metadata.description",
                                  "value_json": json.dumps("Labels every study file for the archive index."),
                                  "evidence": "label every study file"}], "open_questions": []}  # fmt: skip
    script = {"intake": [intake_answer], "test_generation": [EDGES], "rule_implementation": [_impl(code, tests)],
              "rule_review": [APPROVE]}  # fmt: skip
    runner = Scripted(tmp_path / "rec", script)
    first = orchestrate(runner, pdir, requirement, tools=False)
    assert (first.stage, first.details) == ("awaiting_acceptance", {"rules": ["r_label"]})
    with pytest.raises(AgentError) as ei:
        accept_rule(pdir, "r_label", " ")
    assert ei.value.code == "acceptor_missing"
    lock = accept_rule(pdir, "r_label", "@carol-qa")  # a person; all gates incl. mypy, bandit, pytest
    assert set(lock["gates"]) >= {"mypy_strict", "bandit", "test_impl", "owner_cases", "edge_cases"}
    assert not validate_dir(pdir).errors
    done = orchestrate(runner, pdir, None, tools=False)
    assert done.stage == "ready"
    package = Path(done.details["review_package"])
    review = json.loads((package / "review.json").read_text())
    assert review["valid"]
    assert review["custom_rules"]["r_label"]["accepted_by"] == "@carol-qa"
    assert (package / "spec.md").read_text().strip()


def test_accept_refuses_a_stale_review(pdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from factory.cli import main

    code, tests = _code(pdir)
    runner = Scripted(tmp_path / "rec", {"test_generation": [EDGES], "rule_implementation": [_impl(code, tests)],
                                         "rule_review": [APPROVE]})  # fmt: skip
    implement_rule(runner, pdir, "r_label", AuditTrail(pdir), tools=False)
    t = pdir / "rules" / "r_label" / "test_impl.py"
    t.write_text(t.read_text() + "\n# changed after the review\n")
    monkeypatch.chdir(pdir.parent.parent)
    assert main(["--json", "accept", "unit_custom", "r_label", "--by", "@carol-qa"]) == 1
    assert not (pdir / "rules" / "r_label" / "impl.lock.json").exists()
