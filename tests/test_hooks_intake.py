"""Agent guard rails (hooks) and the deterministic half of requirement intake."""
from __future__ import annotations

import json

import pytest

from pf.hooks import pre_tool


@pytest.mark.parametrize("tool,inp,denied", [
    ("Edit", {"file_path": "pipelines/p/build/connector/connector.py"}, True),
    ("Write", {"file_path": "pipelines/p/rules.lock.yaml"}, True),
    ("Write", {"file_path": "pipelines/p/releases/1.0.0.json"}, True),
    ("Write", {"file_path": ".github/workflows/pf-p.yml"}, True),
    ("Write", {"file_path": "x/configuration.json"}, True),
    ("Edit", {"file_path": "pipelines/p/sttm.yaml"}, False),
    ("Bash", {"command": "fivetran deploy pipelines/p/build/connector"}, True),
    ("Bash", {"command": "pf --json deploy p --env prod"}, True),
    ("Bash", {"command": "pf deploy p --env prod --dry-run"}, False),
    ("Bash", {"command": "pf rules accept p r --by @me"}, True),
    ("Bash", {"command": "cat pipelines/p/build/connector/configuration.json"}, True),
    ("Bash", {"command": "echo $FIVETRAN_API_KEY"}, True),
    ("Bash", {"command": "sed -i s/a/b/ pipelines/p/rules.lock.yaml"}, True),
    ("Bash", {"command": "echo x > pipelines/p/build/README.md"}, True),
    ("Bash", {"command": "pf --json build p"}, False),
])
def test_pre_tool_guard(capsys, tool, inp, denied):
    pre_tool({"tool_name": tool, "tool_input": inp})
    out = capsys.readouterr().out
    assert ('"deny"' in out) is denied, out


def test_post_edit_reports_errors(onc, capsys):
    from pf.hooks import post_edit

    text = (onc / "source.yaml").read_text().replace("port: 22", "port: 70000")
    (onc / "source.yaml").write_text(text)
    rc = post_edit({"tool_input": {"file_path": str(onc / "source.yaml")}})
    assert rc == 2 and "source.port" in capsys.readouterr().err


def _proposal(**over):
    from pf.llm.schemas import RequirementProposal

    base = {
        "fields": [
            {"file": "source.yaml", "path": "source.host", "value_json": '"sftp.vendor.example"', "evidence": "host sftp.vendor.example", "confidence": "high"},
            {"file": "source.yaml", "path": "source.port", "value_json": "2222", "evidence": "port 2222", "confidence": "high"},
            {"file": "source.yaml", "path": "source.timeout_seconds", "value_json": '"soon"', "evidence": "x", "confidence": "high"},
            {"file": "destination.yaml", "path": "destination.environments.dev.bucket", "value_json": '"stolen"', "evidence": "x", "confidence": "high"},
            {"file": "policy.yaml", "path": "retention.days", "value_json": "1", "evidence": "x", "confidence": "high"},
            {"file": "pipeline.yaml", "path": "title", "value_json": '"Guess"', "evidence": "x", "confidence": "low"},
        ],
        "entities": [{"name": "visits", "description": "Visits", "member_pattern": "visits_\\d+\\.csv", "table": None,
                      "cursor_column": None, "primary_key": ["visit_id"], "evidence": "one row per visit",
                      "attributes": [
                          {"name": "visit_id", "source_column": "VISIT_ID", "type": "STRING", "precision": None, "scale": None,
                           "input_format": None, "nullable": False, "description": "id", "business_rule": "unique", "checks": [], "evidence": "x"},
                          {"name": "site_no", "source_column": "SITE_NO", "type": "STRING", "precision": None, "scale": None,
                           "input_format": None, "nullable": False, "description": "site", "business_rule": "4 digits", "evidence": "x",
                           "checks": [{"id": "site_no_format", "kind": "regex", "pattern": "^\\d{4}$", "values": [], "min": None,
                                       "max": None, "description": "4 digits", "evidence": "4 digits"}]},
                          {"name": "issues", "source_column": "ISSUES", "type": "INT", "precision": None, "scale": None,
                           "input_format": None, "nullable": True, "description": "n", "business_rule": "never negative", "evidence": "x",
                           "checks": [{"id": "issues_range", "kind": "range", "pattern": None, "values": [], "min": 0.0, "max": None,
                                       "description": ">= 0", "evidence": "never negative"}]}]}],
        "english_rules": [{"entity": "visits", "attribute": None, "id": "cov_zero", "text": "COV visits have 0 issues", "evidence": "x"}],
        "open_questions": ["Retention?"],
    }
    base.update(over)
    return RequirementProposal.model_validate(json.loads(json.dumps(base)))


def test_intake_merge_is_deterministic_and_bounded(repo):
    from pf import yamlio
    from pf.intake import merge
    from pf.scaffold import new_pipeline

    path = new_pipeline(repo, "visits", "sftp", "s3", ["visits"], ["dev"])
    res = merge(path, _proposal(), "req.md")
    applied = {(a.get("file"), a.get("path")) for a in res["applied"] if "path" in a}
    skipped = {(a.get("file"), a.get("path")): a["why"] for a in res["skipped"] if "path" in a}
    assert ("source.yaml", "source.host") in applied and ("source.yaml", "source.port") in applied  # default replaced
    assert "invalid value" in skipped[("source.yaml", "source.timeout_seconds")]                   # rolled back
    assert ("destination.yaml", "destination.environments.dev.bucket") in skipped                  # never from a document
    assert "low confidence" in skipped[("pipeline.yaml", "title")]
    assert any(f["file"] == "policy.yaml" for f in res["for_owners"])                              # governance decides
    sttm = yamlio.to_plain(yamlio.load(path / "sttm.yaml"))
    attrs = {a["name"]: a for a in sttm["entities"][0]["attributes"]}
    assert attrs["site_no"]["checks"][0] == {"id": "site_no_format", "kind": "regex", "pattern": "^\\d{4}$", "description": "4 digits"}
    assert attrs["issues"]["checks"][0]["min"] == 0 and isinstance(attrs["issues"]["checks"][0]["min"], int)
    assert sttm["entities"][0]["row_checks"][0]["kind"] == "english"
    assert "from req.md" in (path / "source.yaml").read_text()  # evidence comments


def test_intake_never_overwrites_a_person(repo):
    from pf.edit import apply_edit
    from pf.intake import merge
    from pf.scaffold import new_pipeline

    path = new_pipeline(repo, "visits", "sftp", "s3", ["visits"], ["dev"])
    apply_edit(path, "source.yaml", "source.host", "set", "chosen.by.person", keep_invalid=True)
    res = merge(path, _proposal(), "req.md")
    assert "chosen.by.person" in (path / "source.yaml").read_text()
    assert any(a.get("path") == "source.host" for a in res["skipped"])
