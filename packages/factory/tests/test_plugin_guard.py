"""The BA plugin's PreToolUse guard: person-only actions, generated files and secrets are refused; it fails closed."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[3] / "plugins" / "factory-ba" / "hooks" / "guard.py"
SM_READ = " ".join(["aws", "secretsmanager", "get-secret-value", "--secret-id", "factory/x"])  # a refused command


def decide(event: object) -> str:
    raw = event if isinstance(event, str) else json.dumps(event)
    proc = subprocess.run(
        [sys.executable, str(GUARD)], input=raw, capture_output=True, text=True, timeout=30, check=True
    )
    out = proc.stdout.strip()
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else "allow"


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("Bash", {"command": "factory --json accept study_pkg_delivery r_x --by @me"}, "deny"),
        ("Bash", {"command": "uv run python -c 'from factory_agents.accept import accept_rule'"}, "deny"),
        ("Bash", {"command": SM_READ}, "deny"),
        ("Bash", {"command": "vault kv get kv/clinops/dev/partner-sftp"}, "deny"),
        ("Bash", {"command": "cat ~/.aws/credentials"}, "deny"),
        ("Bash", {"command": "factory --json validate study_pkg_delivery"}, "allow"),
        ("Bash", {"command": "factory --json set study_pkg_delivery rules.yaml rules[r_x].plain_english 'x'"}, "allow"),
        ("Edit", {"file_path": "/repo/build/study_pkg_delivery/dags/dev/study_pkg_delivery.py"}, "deny"),
        ("Write", {"file_path": "/repo/pipelines/p/rules/r_x/impl.lock.json"}, "deny"),
        ("Write", {"file_path": "/repo/pipelines/p/spec.lock.json"}, "deny"),
        ("Edit", {"file_path": "/repo/pipelines/p/audit/agent-decisions.jsonl"}, "deny"),
        ("Edit", {"file_path": "/repo/pipelines/p/spec/rules.yaml"}, "allow"),
        ("Read", {"file_path": "/home/u/.env"}, "deny"),
        ("Read", {"file_path": "/repo/certs/client.pem"}, "deny"),
        ("Read", {"file_path": "/repo/pipelines/p/spec/pipeline.yaml"}, "allow"),
    ],
)
def test_guard_decisions(tool: str, args: dict[str, str], expected: str) -> None:
    assert decide({"tool_name": tool, "tool_input": args}) == expected


def test_guard_fails_closed() -> None:
    assert decide("not json") == "deny"
    assert decide({"tool_input": {}}) == "deny"


def test_plugin_files_are_well_formed() -> None:
    root = GUARD.parents[1]
    assert json.loads((root / ".claude-plugin" / "plugin.json").read_text())["name"] == "factory-ba"
    hooks = json.loads((root / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"][0]
    assert "guard.py" in hooks["hooks"][0]["command"]
    for f in (root / "skills" / "spec-help" / "SKILL.md", root / "agents" / "spec-reviewer.md"):
        assert f.read_text().startswith("---\nname: ")
    assert "tools: Read, Grep, Glob" in (root / "agents" / "spec-reviewer.md").read_text()
