"""The factory CLI: one JSON envelope per command, exit codes 0/1/2, and the starter lifecycle end to end."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cli_helpers import EXAMPLE_REPO, PLATFORM_SETTINGS, fill_edits
from conftest import Run
from factory.cli import starters

PIPE = "study_pkg_delivery"
STARTERS = ("rest_to_s3", "s3_package_deliver", "s3_to_s3_event", "table_to_s3_fivetran")


def _envelope_ok(env: dict[str, object], command: str) -> None:
    assert env["api_version"] == "factory/v1"
    assert env["command"] == command
    assert set(env) == {"api_version", "command", "ok", "factory_version", "issues", "data"}


# ---------------------------------------------------------------- validate / lock / show on the worked example


def test_validate_example(cli: Run, example_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(example_repo)
    code, env = cli("--json", "validate", PIPE)
    assert code == 0
    _envelope_ok(env, "validate")
    assert env["ok"] is True
    assert env["issues"] == []
    assert env["data"]["logic_sha"].startswith("7670e3f0")


def test_validate_from_a_subfolder_and_human_output(
    cli: Run, example_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(example_repo / "pipelines" / PIPE / "spec")
    code, out = cli("validate", PIPE)
    assert code == 0
    assert out.startswith("validate: OK")


def test_validate_reports_problems_with_exit_1(cli: Run, example_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(example_repo)
    rules = example_repo / "pipelines" / PIPE / "spec" / "rules.yaml"
    rules.write_text(rules.read_text(encoding="utf-8").replace("op: package.zip", "op: package.rar"), encoding="utf-8")
    code, env = cli("--json", "validate", PIPE)
    assert code == 1
    assert env["ok"] is False
    issue = next(i for i in env["issues"] if i["code"] == "op_unknown")
    assert issue["hint"]
    assert issue["loc"] == "rules[r_zip].implementation"
    code, out = cli("validate", PIPE)
    assert code == 1
    assert "op_unknown" in out
    assert "fix: " in out


def test_lock_check_show(cli: Run, example_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(example_repo)
    assert cli("--json", "lock", PIPE, "--check")[0] == 0
    code, env = cli("--json", "show", PIPE, "--view", "lock")
    assert code == 0
    assert env["data"]["lock"]["pipeline_id"] == PIPE
    code, out = cli("show", PIPE)
    assert code == 0
    assert out.startswith("# Study package delivery")
    (example_repo / "pipelines" / PIPE / "spec.lock.json").unlink()
    code, env = cli("--json", "lock", PIPE, "--check")
    assert code == 1
    assert [i["code"] for i in env["issues"]] == ["lock_missing"]
    code, env = cli("--json", "lock", PIPE)
    assert code == 0
    assert env["data"]["lock"] == f"pipelines/{PIPE}/spec.lock.json"
    assert cli("--json", "lock", PIPE, "--check")[0] == 0


def test_set_rolls_back_and_keeps_file(cli: Run, example_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(example_repo)
    f = example_repo / "pipelines" / PIPE / "spec" / "pipeline.yaml"
    before = f.read_text(encoding="utf-8")
    code, env = cli("--json", "set", PIPE, "pipeline.yaml", "orchestration.tasks[t2_checksum].timeout", "2h")
    assert code == 1
    assert [i["code"] for i in env["issues"]] == ["task_timeout_exceeds_engine"]
    assert f.read_text(encoding="utf-8") == before
    code, env = cli("--json", "set", PIPE, "rules.yaml", "rules[r_zip].implementation.params.level", "9")
    assert code == 0
    assert env["ok"] is True


def test_migrate_is_a_no_op_at_current_version(cli: Run, example_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(example_repo)
    code, env = cli("--json", "migrate", PIPE, "--dry-run")
    assert code == 0
    assert env["data"] == {"from": "1.0.0", "to": "1.0.0", "applied": [], "changed": [], "dry_run": True}


def test_schema_check_and_write(cli: Run, tmp_path: Path) -> None:
    code, env = cli("--json", "schema", "--check")
    assert code == 0
    assert env["ok"] is True
    code, env = cli("--json", "schema", "--write", str(tmp_path / "s"))
    assert code == 0
    assert "pipeline.schema.json" in env["data"]["written"]
    assert (tmp_path / "s" / "SCHEMAS.json").is_file()


# ---------------------------------------------------------------- usage and environment errors: exit 2


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("--json", "frobnicate"), "usage"),
        (("--json", "validate"), "usage"),
        (("--json", "show", PIPE, "--view", "xml"), "usage"),
        (("--json", "validate", "no_such_pipeline"), "not_found"),
        (("--json", "new", "Bad-Name"), "usage"),
        (("--json", "new", "x", "--starter", "nope"), "usage"),
        (("--json", "new", PIPE), "usage"),  # exists, no --force
        (("--json", "set", PIPE, "nope.yaml", "a", "1"), "invalid"),
    ],
)
def test_usage_errors_are_one_envelope(
    cli: Run, example_repo: Path, monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...], code: str
) -> None:
    monkeypatch.chdir(example_repo)
    rc, env = cli(*args)
    assert env["ok"] is False
    assert env["issues"][0]["code"] == code
    assert rc == (1 if code == "invalid" else 2)


def test_outside_a_repository(cli: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    rc, env = cli("--json", "validate", PIPE)
    assert rc == 2
    assert env["issues"][0]["code"] == "no_repo"


# ---------------------------------------------------------------- init and starters, end to end


def test_init_is_idempotent(cli: Run, tmp_path: Path) -> None:
    root = tmp_path / "team"
    for _ in range(2):
        rc, _env = cli("--json", "init", str(root), "--team", "clinops_data")
        assert rc == 0
    assert (root / "pipelines").is_dir()
    assert 'team = "clinops_data"' in (root / "factory.toml").read_text(encoding="utf-8")
    lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(set(lines))


def test_starters_ship_as_package_data() -> None:
    assert tuple(starters()) == STARTERS
    for s in starters().values():
        assert (s / "starter.toml").is_file()
        assert sorted(p.name for p in (s / "spec").glob("*.yaml")) == [
            "destinations.yaml",
            "environments.yaml",
            "integrations.yaml",
            "pipeline.yaml",
            "rules.yaml",
            "security.yaml",
        ]


@pytest.mark.parametrize("starter", STARTERS)
def test_starter_fill_validate_lock(cli: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, starter: str) -> None:
    """new -> every placeholder filled with `factory set` -> valid with no findings -> lock -> lock --check."""
    root = tmp_path / "team"
    cli("--json", "init", str(root), "--team", "clinops_data", "--platform-settings", PLATFORM_SETTINGS.as_posix())
    monkeypatch.chdir(root)
    name = f"p_{starter}"
    rc, env = cli("--json", "new", name, "--starter", starter)
    assert rc == 0
    assert env["issues"] == []  # a fresh starter has placeholders only
    edits = fill_edits(starter)
    assert env["data"]["to_fill"] == len(edits)
    rc, env = cli("--json", "validate", name)
    assert rc == 1  # placeholders are errors without --allow-unfilled
    assert {i["code"] for i in env["issues"]} == {"required"}
    assert cli("--json", "validate", name, "--allow-unfilled")[0] == 0
    for fname, path, value in edits:
        rc, env = cli("--json", "set", name, fname, path, value)
        assert rc == 0, (fname, path, env["issues"])
    rc, env = cli("--json", "validate", name)
    assert (rc, env["issues"]) == (0, [])
    assert env["data"]["unfilled"] == 0
    assert cli("--json", "lock", name)[0] == 0
    assert cli("--json", "lock", name, "--check")[0] == 0
    rc, out = cli("show", name)
    assert rc == 0
    assert "<REQUIRED>" not in out


# ---------------------------------------------------------------- the installed console script


def test_console_script_smoke(tmp_path: Path) -> None:
    from cli_helpers import copy_example

    repo = copy_example(tmp_path / "repo")
    exe = Path(sys.executable).parent / "factory"
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    ok = subprocess.run([str(exe), "--json", "validate", PIPE], cwd=repo, env=env, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["ok"] is True
    bad = subprocess.run([str(exe), "--json", "frobnicate"], cwd=repo, env=env, capture_output=True, text=True)
    assert bad.returncode == 2
    assert json.loads(bad.stdout)["issues"][0]["code"] == "usage"
    assert EXAMPLE_REPO.is_dir()
