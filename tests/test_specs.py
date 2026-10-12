"""Spec models, scaffolds, cross-validation and edits."""
from __future__ import annotations

import pytest

from pf import yamlio
from pf.bundle import load_bundle
from pf.edit import apply_edit
from pf.scaffold import new_pipeline


def errors(b, *, ignore_required=True):
    return [i for i in b.issues if i.severity == "error" and not (ignore_required and i.code == "required")]


@pytest.mark.parametrize("src", ["sftp", "ftp", "rds"])
def test_scaffold_only_needs_values(repo, src):
    path = new_pipeline(repo, f"p_{src}", src, "s3", ["things"], ["dev", "prod"])
    b = load_bundle(path)
    assert errors(b) == [], errors(b)
    assert any(i.code == "required" for i in b.issues)  # the fill-in fields are reported, not defaulted


def test_scaffold_rejects_unknown_types(repo):
    from pf.envelope import PfError

    with pytest.raises(PfError):
        new_pipeline(repo, "p", "s3", "s3", ["t"], ["dev"])
    with pytest.raises(PfError):
        new_pipeline(repo, "p", "sftp", "snowflake", ["t"], ["dev"])


def test_example_pipeline_is_valid(onc):
    b = load_bundle(onc)
    assert b.ok and errors(b, ignore_required=False) == []


def test_extra_keys_are_rejected(onc):
    doc = yamlio.load(onc / "source.yaml")
    doc["source"]["hostname"] = "typo"
    yamlio.dump(doc, onc / "source.yaml")
    b = load_bundle(onc)
    assert any(i.loc.startswith("source") and i.code == "invalid" for i in b.issues)


def test_letter_digit_names_are_rejected(onc):
    issues = apply_edit(onc, "sttm.yaml", "entities[lab_results].attributes[result_unit].name", "set", "result_unit2", keep_invalid=True)
    assert any("result_unit_2" in i.msg for i in issues)


def test_invalid_edit_rolls_back(onc):
    from pf.envelope import PfError

    before = (onc / "source.yaml").read_text()
    with pytest.raises(PfError) as e:
        apply_edit(onc, "source.yaml", "source.port", "set", "not-a-port")
    assert e.value.code == "invalid" and e.value.issues
    assert (onc / "source.yaml").read_text() == before


def test_edit_keeps_comments(onc):
    apply_edit(onc, "source.yaml", "source.timeout_seconds", "set", "90")
    text = (onc / "source.yaml").read_text()
    assert "timeout_seconds: 90" in text and "#" in text


@pytest.mark.parametrize("mode,tier,severity", [("disable", "gxp_direct", "error"), ("require", "gxp_direct", "warning"),
                                                ("disable", "non_gxp", "warning"), ("verify-full", "gxp_direct", None)])
def test_rds_tls_policy(repo, mode, tier, severity):
    import shutil

    from tests.conftest import FRAMEWORK

    shutil.copytree(FRAMEWORK / "examples" / "pipelines" / "trial_sites", repo / "pipelines" / "trial_sites", ignore=shutil.ignore_patterns("build"))
    path = repo / "pipelines" / "trial_sites"
    apply_edit(path, "source.yaml", "source.ssl_mode", "set", mode, keep_invalid=True)
    apply_edit(path, "pipeline.yaml", "gxp_tier", "set", tier, keep_invalid=True)
    tls = [i for i in load_bundle(path).issues if i.loc == "source.ssl_mode"]
    assert (tls[0].severity if tls else None) == severity


def test_sql_assert_must_be_select():
    from pf.specs.quality import QualityFile

    with pytest.raises(Exception):
        QualityFile.model_validate({"ticket_route": "x", "checks": [
            {"id": "q", "kind": "sql_assert", "entity": "e", "sql": "DELETE FROM t", "severity": "blocker", "description": "d"}]})


@pytest.mark.parametrize("example,file,path,value,needle", [
    ("trial_sites", "sttm.yaml", "entities[sites].attributes[updated_at].nullable", "true", "cursor column must be nullable: false"),
    ("trial_sites", "sttm.yaml", "entities[sites].checks", "[{id: u, kind: unique, columns: [site_id], description: check under test}]", "file sources only"),
    ("oncology_labs", "source.yaml", "source.package.on_redelivery", "reprocess", None),
    ("oncology_labs", "sttm.yaml", "entities[lab_results].attributes[site_id].checks[site_id_format]",
     "{id: site_id_format, kind: range, min: 1, max: 10, description: check under test}", "not a STRING value"),
    ("oncology_labs", "sttm.yaml", "entities[lab_results].attributes[result_value].scale", "12", "scale <= 10"),
    ("oncology_labs", "sttm.yaml", "entities[lab_results].attributes[result_unit].name", "result__unit", "result_unit"),
    ("oncology_labs", "policy.yaml", "retention.object_lock_days", "100", "must be at least retention days"),
])
def test_unsafe_combinations_rejected(repo, example, file, path, value, needle):
    """Runtime-unsafe combinations found in the independent review are rejected before build."""
    import shutil

    from tests.conftest import FRAMEWORK

    if example != "oncology_labs":
        shutil.copytree(FRAMEWORK / "examples" / "pipelines" / example, repo / "pipelines" / example, ignore=shutil.ignore_patterns("build"))
    pth = repo / "pipelines" / example
    if example == "oncology_labs" and path == "source.package.on_redelivery":
        apply_edit(pth, "sttm.yaml", "entities[lab_results].load", "set", "append", keep_invalid=True)
        needle = "keeps the old rows"
    issues = apply_edit(pth, file, path, "set", value, keep_invalid=True)
    msgs = " | ".join(i.msg for i in load_bundle(pth).issues if i.severity == "error") + " | ".join(i.msg for i in issues)
    assert needle in msgs, msgs


def test_yaml_aliases_rejected(onc):
    text = (onc / "quality.yaml").read_text()
    (onc / "quality.yaml").write_text("x: &a [1, 2]\ny: *a\n" + text)
    b = load_bundle(onc)
    assert any("aliases" in i.msg for i in b.issues), [i.msg for i in b.issues][:3]
