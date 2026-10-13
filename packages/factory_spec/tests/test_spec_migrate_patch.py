"""Schema migrations (with a test-only 1.0.0 -> 1.1.0 migration) and structured spec patches with rollback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from factory_core.envelope import CommandError
from factory_spec.migrate import CURRENT_SCHEMA_VERSION, DEFAULT_REGISTRY, Migration, Registry, migrate_dir
from factory_spec.patch import SpecPatch, apply_patch, parse_value
from factory_spec.validate import validate_dir

from factory_core import yamlio

# ---------------------------------------------------------------- migrations


def _rename_runs_per_day(docs: dict[str, Any]) -> None:
    wl = docs["pipeline.yaml"]["workload"]
    wl.insert(list(wl).index("runs_per_day"), "runs_per_day_expected", wl.pop("runs_per_day"))


def _registry() -> Registry:
    r = Registry()
    r.register(Migration("1.0.0", "1.1.0", "rename workload.runs_per_day", _rename_runs_per_day))
    return r


def test_default_registry_is_at_current_version() -> None:
    assert DEFAULT_REGISTRY.latest == CURRENT_SCHEMA_VERSION == "1.0.0"


def test_migration_chain_applies_and_keeps_comments(pipeline_dir: Path) -> None:
    f = pipeline_dir / "spec" / "pipeline.yaml"
    before = f.read_text(encoding="utf-8")
    report = migrate_dir(pipeline_dir, registry=_registry())
    after = f.read_text(encoding="utf-8")
    assert (report.from_version, report.to_version) == ("1.0.0", "1.1.0")
    assert report.changed_files == ["pipeline.yaml"]
    assert report.applied == ["1.0.0 -> 1.1.0: rename workload.runs_per_day"]
    assert "schema_version: 1.1.0" in after
    assert "runs_per_day_expected: 6" in after
    # every comment line survives
    assert [ln for ln in before.splitlines() if ln.lstrip().startswith("#")] == [
        ln for ln in after.splitlines() if ln.lstrip().startswith("#")
    ]


def test_migration_dry_run_writes_nothing(pipeline_dir: Path) -> None:
    f = pipeline_dir / "spec" / "pipeline.yaml"
    before = f.read_text(encoding="utf-8")
    report = migrate_dir(pipeline_dir, registry=_registry(), dry_run=True)
    assert report.changed_files == ["pipeline.yaml"]
    assert f.read_text(encoding="utf-8") == before


def test_migration_without_path_and_double_registration(pipeline_dir: Path) -> None:
    with pytest.raises(CommandError) as ei:
        migrate_dir(pipeline_dir, to="2.0.0", registry=_registry())
    assert ei.value.code == "migration"
    r = _registry()
    with pytest.raises(ValueError, match="already registered"):
        r.register(Migration("1.0.0", "1.2.0", "again", lambda docs: None))


def test_migration_at_latest_is_a_no_op(pipeline_dir: Path) -> None:
    report = migrate_dir(pipeline_dir)
    assert report.applied == []
    assert report.changed_files == []


# ---------------------------------------------------------------- patches


def _text(d: Path, fname: str) -> str:
    return (d / "spec" / fname).read_text(encoding="utf-8")


def test_set_keeps_comments_and_validates(pipeline_dir: Path) -> None:
    before = _text(pipeline_dir, "rules.yaml")
    out = apply_patch(pipeline_dir, SpecPatch("rules.yaml", "set", "rules[r_zip].implementation.params.level", 9))
    after = _text(pipeline_dir, "rules.yaml")
    assert out.issues == []
    assert "level: 9" in after
    assert after.splitlines()[:2] == before.splitlines()[:2]  # header comments kept
    assert len(after.splitlines()) == len(before.splitlines())
    assert validate_dir(pipeline_dir).ok


def test_invalid_edit_is_rolled_back(pipeline_dir: Path) -> None:
    before = _text(pipeline_dir, "pipeline.yaml")
    with pytest.raises(CommandError) as ei:
        apply_patch(pipeline_dir, SpecPatch("pipeline.yaml", "set", "orchestration.tasks[t2_checksum].timeout", "2h"))
    assert ei.value.code == "invalid"
    assert [i.code for i in ei.value.issues] == ["task_timeout_exceeds_engine"]
    assert _text(pipeline_dir, "pipeline.yaml") == before


def test_keep_invalid_writes_and_reports(pipeline_dir: Path) -> None:
    out = apply_patch(
        pipeline_dir,
        SpecPatch("pipeline.yaml", "set", "orchestration.tasks[t2_checksum].timeout", "2h"),
        keep_invalid=True,
    )
    assert [i.code for i in out.issues] == ["task_timeout_exceeds_engine"]
    assert "timeout: 2h" in _text(pipeline_dir, "pipeline.yaml")


def test_add_and_remove(pipeline_dir: Path) -> None:
    apply_patch(
        pipeline_dir, SpecPatch("security.yaml", "add", "security_and_access.residency.allowed_regions", "eu-central-1")
    )
    doc = yamlio.load_path(pipeline_dir / "spec" / "security.yaml")
    assert list(doc["security_and_access"]["residency"]["allowed_regions"]) == ["eu-west-1", "eu-central-1"]
    apply_patch(pipeline_dir, SpecPatch("pipeline.yaml", "remove", "metadata.tags"))
    assert "tags:" not in _text(pipeline_dir, "pipeline.yaml")
    apply_patch(pipeline_dir, SpecPatch("security.yaml", "remove", "security_and_access.residency.allowed_regions[1]"))
    doc = yamlio.load_path(pipeline_dir / "spec" / "security.yaml")
    assert list(doc["security_and_access"]["residency"]["allowed_regions"]) == ["eu-west-1"]


def test_selectors_by_id_in_binding_files(pipeline_dir: Path) -> None:
    apply_patch(
        pipeline_dir,
        SpecPatch("destinations.yaml", "set", "destinations.prod[partner_sftp].username", "acme-delivery-2"),
    )
    assert "username: acme-delivery-2" in _text(pipeline_dir, "destinations.yaml")


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        (SpecPatch("nope.yaml", "set", "a", 1), "invalid"),
        (SpecPatch("rules.yaml", "set", "rules[r_nope].plain_english", "x"), "not_found"),
        (SpecPatch("rules.yaml", "remove", "rules[r_zip].nothing_here"), "not_found"),
        (SpecPatch("rules.yaml", "set", "rules[99].rule_id", "x"), "not_found"),
        (SpecPatch("rules.yaml", "set", "rules..x", 1), "invalid"),
        (SpecPatch("pipeline.yaml", "add", "metadata.name", "x"), "invalid"),
    ],
)
def test_patch_errors(pipeline_dir: Path, patch: SpecPatch, code: str) -> None:
    before = _text(pipeline_dir, "rules.yaml")
    with pytest.raises(CommandError) as ei:
        apply_patch(pipeline_dir, patch)
    assert ei.value.code == code
    assert _text(pipeline_dir, "rules.yaml") == before


def test_parse_value() -> None:
    assert parse_value("9") == 9
    assert parse_value("[a, b]") == ["a", "b"]
    assert parse_value("") is None
    with pytest.raises(CommandError):
        parse_value("[a")
