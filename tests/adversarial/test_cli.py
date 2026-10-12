"""CLI: error handling, --json envelope contract, invalid names, build determinism, release/versioning."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

FRAMEWORK = Path(__file__).resolve().parents[2]
PF = "/home/claude/pipeline-factory/.venv/bin/pf"


def pf(cwd: Path, *args: str, json_mode: bool = True, stdin: str | None = None):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "PF_AGENT_SESSION")}
    env.update(PYTHONPATH=str(FRAMEWORK / "src"), PF_LLM_PROVIDER="replay", PF_LLM_FIXTURES=str(FRAMEWORK / "tests/llm-fixtures"),
               PATH="/home/claude/pipeline-factory/.venv/bin:" + env.get("PATH", ""))
    cmd = [PF] + (["--json"] if json_mode else []) + list(args)
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, input=stdin, timeout=300)
    return p


def envelope(p) -> dict:
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"--json did not print one JSON envelope (rc={p.returncode}): stdout={p.stdout[:400]!r} stderr={p.stderr[-600:]!r}")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "pf.toml").write_text("")
    for n in ("oncology_labs", "site_monitoring", "trial_sites"):
        shutil.copytree(FRAMEWORK / "examples/pipelines" / n, tmp_path / "pipelines" / n, ignore=shutil.ignore_patterns("build", "intake", "releases"))
    return tmp_path


def test_missing_pipeline(repo):
    p = pf(repo, "validate", "nope")
    e = envelope(p)
    assert e["ok"] is False and p.returncode in (1, 2)


@pytest.mark.parametrize("name", ["../../escape", "/tmp/abs", "Bad-Name", "a" * 80, ""])
def test_invalid_pipeline_names_rejected(repo, name):
    p = pf(repo, "new", name, "--source", "sftp", "--destination", "s3")
    e = envelope(p)
    assert e["ok"] is False, f"name {name!r} accepted: {e.get('data')}"
    assert not (repo.parent / "escape").exists()


def test_bad_yaml(repo):
    (repo / "pipelines/site_monitoring/source.yaml").write_text("source: [unclosed\n  - : :\n")
    e = envelope(pf(repo, "validate", "site_monitoring"))
    assert e["ok"] is False and any(i["code"] == "yaml" for i in e["issues"])


def test_missing_spec_file(repo):
    (repo / "pipelines/site_monitoring/quality.yaml").unlink()
    e = envelope(pf(repo, "validate", "site_monitoring"))
    assert e["ok"] is False and any(i["code"] == "missing_file" for i in e["issues"])


def test_yaml_billion_laughs(repo):
    bomb = "a: &a [x,x,x,x,x,x,x,x,x]\n" + "".join(f"{chr(98+i)}: &{chr(98+i)} [*{chr(97+i)},*{chr(97+i)},*{chr(97+i)},*{chr(97+i)},*{chr(97+i)},*{chr(97+i)},*{chr(97+i)},*{chr(97+i)},*{chr(97+i)}]\n" for i in range(8))
    (repo / "pipelines/site_monitoring/quality.yaml").write_text(bomb)
    p = pf(repo, "validate", "site_monitoring")
    envelope(p)


def test_set_invalid_value_rolls_back(repo):
    f = repo / "pipelines/site_monitoring/pipeline.yaml"
    before = f.read_text()
    e = envelope(pf(repo, "set", "site_monitoring", "pipeline.yaml", "gxp_tier", "super_gxp"))
    assert e["ok"] is False and f.read_text() == before


def test_set_unknown_key_typo(repo):
    e = envelope(pf(repo, "set", "site_monitoring", "pipeline.yaml", "gxp_teir", "non_gxp"))
    assert e["ok"] is False


def test_add_check_duplicate_id(repo):
    e = envelope(pf(repo, "add-check", "site_monitoring", "--entity", "site_monitoring", "--attribute", "site_no",
                    "{id: site_no_format, kind: not_null, description: dup}"))
    assert e["ok"] is False


def test_unknown_command_json(repo):
    p = pf(repo, "frobnicate")
    assert p.returncode == 2
    envelope(p)


def test_missing_required_arg_json(repo):
    p = pf(repo, "deploy", "site_monitoring")  # --env missing
    assert p.returncode == 2
    envelope(p)


def test_outside_repo(tmp_path):
    e = envelope(pf(tmp_path, "validate", "x"))
    assert e["ok"] is False


def test_catalog_lists_all_kinds(repo):
    e = envelope(pf(repo, "catalog"))
    blob = json.dumps(e["data"])
    for k in ["not_null", "regex", "allowed_values", "range", "length", "expression", "python", "english", "row_count", "unique",
              "control_count", "checksum", "duplicate_package", "file_size", "pseudonymise", "map_values", "pad_left", "derive", "unzip", "gunzip"]:
        assert f'"{k}"' in blob, k


def _tree_hash(d: Path) -> dict:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


@pytest.mark.parametrize("name", ["oncology_labs", "site_monitoring", "trial_sites"])
def test_build_deterministic(repo, name, tmp_path_factory):
    assert envelope(pf(repo, "build", name))["ok"]
    h1 = _tree_hash(repo / "pipelines" / name / "build")
    # build again in a different absolute path + different cwd + different TZ/locale
    other = tmp_path_factory.mktemp("other") / "deeper"
    shutil.copytree(repo, other)
    shutil.rmtree(other / "pipelines" / name / "build")
    env_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Kolkata"
    try:
        assert envelope(pf(other / "pipelines", "build", name))["ok"]
    finally:
        if env_tz is None:
            os.environ.pop("TZ")
        else:
            os.environ["TZ"] = env_tz
    h2 = _tree_hash(other / "pipelines" / name / "build")
    diff = sorted(k for k in set(h1) | set(h2) if h1.get(k) != h2.get(k))
    assert not diff, diff
    assert envelope(pf(repo, "build", name, "--check"))["ok"]


def test_build_check_detects_hand_edit(repo):
    assert envelope(pf(repo, "build", "site_monitoring"))["ok"]
    f = repo / "pipelines/site_monitoring/build/connector/connector.py"
    f.write_text(f.read_text() + "\n# tampered\n")
    assert envelope(pf(repo, "build", "site_monitoring", "--check"))["ok"] is False


def test_build_check_detects_added_file(repo):
    assert envelope(pf(repo, "build", "site_monitoring"))["ok"]
    (repo / "pipelines/site_monitoring/build/connector/pf_runtime/evil.py").write_text("import os\n")
    assert envelope(pf(repo, "build", "site_monitoring", "--check"))["ok"] is False, "extra file in the vendored runtime not detected"


def _release_flow(repo, name="site_monitoring"):
    assert envelope(pf(repo, "build", name))["ok"]
    assert envelope(pf(repo, "test", name))["ok"]
    return envelope(pf(repo, "release", name))


def test_release_then_change_needs_bump(repo):
    r = _release_flow(repo)
    assert r["ok"], r
    # semantic change: make a nullable attribute non-nullable (stricter -> rows that loaded before now quarantine)
    e = envelope(pf(repo, "set", "site_monitoring", "sttm.yaml", "entities[site_monitoring].attributes[cra_email].nullable", "false"))
    assert e["ok"], e
    assert envelope(pf(repo, "build", "site_monitoring"))["ok"]
    chk = envelope(pf(repo, "release", "site_monitoring", "--check"))
    assert chk["ok"] is False


def test_release_pseudonymise_added_to_key_needs_major(repo):
    """Adding pseudonymise to a primary-key column rewrites every key (old rows orphaned): needs a major bump."""
    r = _release_flow(repo)
    assert r["ok"], r
    e = envelope(pf(repo, "add", "site_monitoring", "sttm.yaml", "entities[site_monitoring].attributes[visit_id].transforms",
                    "{kind: pseudonymise, key: {secret: visit_salt}}"))
    assert e["ok"], e
    v = envelope(pf(repo, "set", "site_monitoring", "pipeline.yaml", "version", "0.2.0"))
    assert v["ok"], v
    assert envelope(pf(repo, "build", "site_monitoring"))["ok"]
    chk = envelope(pf(repo, "release", "site_monitoring"))
    assert chk["ok"] is False, f"minor bump accepted for a key-rewriting change: {chk.get('data')}"


def test_release_same_version_after_framework_change(repo):
    """Same spec, different vendored runtime: the same version must not be re-released with different bytes."""
    r1 = _release_flow(repo)
    assert r1["ok"], r1
    zsha1 = r1["data"]["connector_zip_sha256"]
    # simulate a framework upgrade: edit the vendored runtime source the build copies
    eng = FRAMEWORK / "src/pf_runtime/logx.py"
    orig = eng.read_text()
    try:
        eng.write_text(orig + "\n# framework patch\n")
        assert envelope(pf(repo, "build", "site_monitoring"))["ok"]
        r2 = envelope(pf(repo, "release", "site_monitoring"))
    finally:
        eng.write_text(orig)
    assert not (r2["ok"] and r2["data"]["connector_zip_sha256"] != zsha1), \
        "version 0.1.0 re-released with a different connector zip (artefact and release record overwritten)"


def test_release_corrupt_record_ignored(repo):
    r = _release_flow(repo)
    assert r["ok"]
    rec = repo / "pipelines/site_monitoring/releases/0.1.0.json"
    rec.write_text("{not json")
    e = envelope(pf(repo, "set", "site_monitoring", "sttm.yaml", "entities[site_monitoring].attributes[cra_email].nullable", "false"))
    assert envelope(pf(repo, "build", "site_monitoring"))["ok"]
    chk = envelope(pf(repo, "release", "site_monitoring"))
    assert chk["ok"] is False, "corrupt release record silently ignored: 0.1.0 re-released with different content"
