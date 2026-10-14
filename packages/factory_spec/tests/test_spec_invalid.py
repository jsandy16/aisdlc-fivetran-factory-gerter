"""Invalid-spec corpus: the valid worked example plus one targeted change per case must raise the expected code.

Every registered code is either produced by a case here or listed in COVERED_ELSEWHERE with the test that covers it.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from factory_spec.codes import CODES
from factory_spec.validate import validate_dir
from spec_helpers import edit, rule, task

Case = Callable[[Path], None]
CASES: dict[str, tuple[str, Case]] = {}


def case(name: str, code: str) -> Callable[[Case], Case]:
    def register(fn: Case) -> Case:
        CASES[name] = (code, fn)
        return fn

    return register


def on(fname: str, mutate: Callable[[Any], None]) -> Case:
    return lambda d: edit(d, fname, mutate)


def simple(name: str, code: str, fname: str, mutate: Callable[[Any], None]) -> None:
    CASES[name] = (code, on(fname, mutate))


def write(fname: str, text: str) -> Case:
    return lambda d: (d / "spec" / fname).write_text(text, encoding="utf-8")


def append(fname: str, text: str) -> Case:
    def run(d: Path) -> None:
        f = d / "spec" / fname
        f.write_text(f.read_text(encoding="utf-8") + text, encoding="utf-8")

    return run


def setitem(path: list[Any], value: Any) -> Callable[[Any], None]:
    def mutate(doc: Any) -> None:
        node = doc
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value

    return mutate


def delitem(path: list[Any]) -> Callable[[Any], None]:
    def mutate(doc: Any) -> None:
        node = doc
        for k in path[:-1]:
            node = node[k]
        del node[path[-1]]

    return mutate


# ---------------------------------------------------------------- load
CASES["missing_file"] = ("missing_file", lambda d: (d / "spec" / "security.yaml").unlink())
CASES["yaml_syntax"] = ("yaml_syntax", write("rules.yaml", "rules: [1\n"))
CASES["yaml_alias"] = ("yaml_alias", append("pipeline.yaml", "x_a: &a 1\nx_b: *a\n"))
CASES["yaml_tag"] = ("yaml_tag", append("pipeline.yaml", "x_t: !custom 1\n"))
CASES["yaml_duplicate_key"] = ("yaml_duplicate_key", append("pipeline.yaml", "cost: {budget_per_run_usd: '1.00'}\n"))
CASES["yaml_date"] = ("yaml_date", append("pipeline.yaml", "x_date: 2024-03-01\n"))
CASES["yaml_key_type"] = ("yaml_key_type", append("pipeline.yaml", "1: x\n"))
CASES["yaml_too_large"] = ("yaml_too_large", append("rules.yaml", ("# " + "x" * 98 + "\n") * 11_000))
simple("required", "required", "pipeline.yaml", setitem(["metadata", "name"], "<REQUIRED>"))
simple("required_field", "required_field", "pipeline.yaml", delitem(["trigger", "queue"]))
simple("unknown_field", "unknown_field", "pipeline.yaml", setitem(["metadata", "nmae"], "typo"))
simple("invalid_choice", "invalid_choice", "pipeline.yaml", setitem(["trigger", "mode"], "batch"))
simple("invalid_value", "invalid_value", "pipeline.yaml", setitem(["workload", "runs_per_day"], -1))
simple(
    "template_unquoted", "template_unquoted", "pipeline.yaml", setitem(["reliability", "run_key"], {"request_id": None})
)
simple("quote_value", "quote_value", "pipeline.yaml", setitem(["metadata", "name"], 123))
simple("api_version_mismatch", "api_version_mismatch", "pipeline.yaml", setitem(["api_version"], "factory/v2"))
simple("schema_unsupported", "schema_version_unsupported", "pipeline.yaml", setitem(["schema_version"], "2.0.0"))
simple("schema_outdated", "schema_version_outdated", "pipeline.yaml", setitem(["schema_version"], "0.9.0"))
simple("folder_mismatch", "pipeline_id_folder_mismatch", "pipeline.yaml", setitem(["metadata", "pipeline_id"], "other"))
simple("open_questions", "open_questions_pending", "pipeline.yaml", setitem(["open_questions"], ["Who owns y?"]))
simple("team_mismatch", "team_mismatch", "pipeline.yaml", setitem(["metadata", "team"], "other_team"))
simple(
    "cel_invalid",
    "cel_invalid",
    "rules.yaml",
    lambda doc: rule(doc, "r_check_listing")["implementation"].update(cel="a =="),
)
simple(
    "jmespath_invalid",
    "jmespath_invalid",
    "integrations.yaml",
    lambda doc: doc["portal"]["endpoints"][0]["mapping"]["destinations"].update(x="targets[?"),
)
simple(
    "template_syntax",
    "template_syntax",
    "rules.yaml",
    lambda doc: rule(doc, "r_package_name")["implementation"]["params"].update(template="{study_id.zip"),
)
simple(
    "pattern_invalid",
    "destination_pattern_invalid",
    "destinations.yaml",
    lambda doc: doc["destinations"]["dev"][0].update(prefix_pattern="studies/**/x"),
)
simple(
    "pattern_too_broad",
    "destination_pattern_too_broad",
    "destinations.yaml",
    lambda doc: doc["destinations"]["dev"][0].update(prefix_pattern="**"),
)

# ---------------------------------------------------------------- ops
simple(
    "op_unknown", "op_unknown", "rules.yaml", lambda doc: rule(doc, "r_zip")["implementation"].update(op="package.rar")
)
simple(
    "op_category_mismatch",
    "op_category_mismatch",
    "rules.yaml",
    lambda doc: rule(doc, "r_zip")["implementation"].update(op="s3.list"),
)
simple(
    "op_not_available",
    "op_not_available",
    "rules.yaml",
    lambda doc: rule(doc, "r_zip")["implementation"].update(op="package.tar", params={}),
)

# ---------------------------------------------------------------- references
simple("duplicate_id", "duplicate_id", "rules.yaml", lambda doc: rule(doc, "r_upload_y").update(rule_id="r_upload_x"))
simple(
    "unresolved_rule",
    "unresolved_reference",
    "pipeline.yaml",
    lambda doc: task(doc, "t3_upload")["rules"].append("r_nope"),
)
simple(
    "rule_unassigned",
    "rule_unassigned",
    "pipeline.yaml",
    lambda doc: task(doc, "t1_collect")["rules"].remove("r_check_listing"),
)
simple(
    "rule_assigned_twice",
    "rule_assigned_twice",
    "pipeline.yaml",
    lambda doc: task(doc, "t3_upload")["rules"].append("r_checksum"),
)
simple(
    "task_cycle", "task_cycle", "pipeline.yaml", lambda doc: task(doc, "t1_collect").update(depends_on=["t3_upload"])
)
simple(
    "secret_kind_mismatch",
    "secret_kind_mismatch",
    "integrations.yaml",
    lambda doc: doc["secrets"]["portal_client"].update(kind="api_key"),
)
simple(
    "secret_unused",
    "secret_unused",
    "integrations.yaml",
    lambda doc: doc["secrets"].update(spare={"provider": "vault", "path": "clinops/{env}/spare", "kind": "generic"}),
)
# AKIAIOSFODNN7EXAMPLE is AWS's documented example access key id, not a credential
CASES["secret_in_spec"] = ("secret_in_spec", append("integrations.yaml", "# aws key AKIAIOSFODNN7EXAMPLE\n"))
simple(
    "variable_unresolvable",
    "variable_unresolvable",
    "pipeline.yaml",
    setitem(["variables", "study_id"], "trigger.message.nope"),
)

# ---------------------------------------------------------------- dataflow
simple("dataflow_gap", "dataflow_gap", "rules.yaml", lambda doc: rule(doc, "r_zip").update(inputs=["nothing_here"]))
simple(
    "dataflow_multiple_producers",
    "dataflow_multiple_producers",
    "rules.yaml",
    lambda doc: rule(doc, "r_upload_y").update(outputs=["delivered_x"]),
)
simple("dataflow_order", "dataflow_order", "rules.yaml", lambda doc: rule(doc, "r_download").update(inputs=["package"]))
simple(
    "artifact_kind_mismatch",
    "artifact_kind_mismatch",
    "rules.yaml",
    lambda doc: rule(doc, "r_zip").update(inputs=["listing"]),
)
simple(
    "output_arity_mismatch",
    "output_arity_mismatch",
    "rules.yaml",
    lambda doc: rule(doc, "r_zip").update(outputs=["package", "extra"]),
)


@case("dataflow_not_staged", "dataflow_not_staged")
def _not_staged(d: Path) -> None:
    def mutate(doc: Any) -> None:
        task(doc, "t1_collect")["rules"].remove("r_zip")
        task(doc, "t2_checksum")["rules"].insert(0, "r_zip")

    edit(d, "pipeline.yaml", mutate)


@case("artifact_unused", "artifact_unused")
def _unused(d: Path) -> None:
    def add_rule(doc: Any) -> None:
        r = copy.deepcopy(rule(doc, "r_package_name"))
        r["rule_id"] = "r_spare_name"
        r["outputs"] = ["spare_name"]
        doc["rules"].append(r)

    edit(d, "rules.yaml", add_rule)
    edit(d, "pipeline.yaml", lambda doc: task(doc, "t3_upload")["rules"].append("r_spare_name"))


# ---------------------------------------------------------------- templates and expressions
simple(
    "template_unknown_token",
    "template_unknown_token",
    "rules.yaml",
    lambda doc: rule(doc, "r_package_name")["implementation"]["params"].update(template="{nope}.zip"),
)
simple(
    "template_token_not_allowed",
    "template_token_not_allowed",
    "integrations.yaml",
    lambda doc: doc["secrets"]["portal_client"].update(path="clinops/{study_id}/portal-client"),
)
simple(
    "template_token_unconstrained",
    "template_token_unconstrained",
    "pipeline.yaml",
    lambda doc: doc["trigger"]["message"]["fields"]["study_id"].pop("pattern"),
)
simple("run_key_invalid", "run_key_invalid", "pipeline.yaml", setitem(["reliability", "run_key"], "{run_date}"))
simple(
    "cel_unknown_name",
    "cel_unknown_name",
    "rules.yaml",
    lambda doc: rule(doc, "r_check_listing")["implementation"].update(cel="artifacts.nope.count == 1"),
)

# ---------------------------------------------------------------- triggers and sources
simple(
    "event_trigger_without_queue",
    "event_trigger_without_queue",
    "environments.yaml",
    setitem(["environments", "dev", "queues"], {"other_queue": {"name": "clinops-other-dev"}}),
)
simple(
    "trigger_field_missing",
    "trigger_field_missing",
    "pipeline.yaml",
    setitem(["trigger", "message", "fields", "bucket"], {"type": "string", "required": False}),
)
simple(
    "recursive_without_include",
    "recursive_without_include",
    "rules.yaml",
    lambda doc: rule(doc, "r_list_objects")["implementation"]["params"].update(include=[]),
)
simple(
    "source_unconstrained",
    "source_unconstrained",
    "environments.yaml",
    setitem(["environments", "dev", "sources", "landing", "buckets"], []),
)
simple("binding_missing", "binding_missing", "environments.yaml", delitem(["environments", "dev", "portal"]))
simple(  # a resource by configuration with a parameter its catalog type does not have
    "resource_invalid",
    "resource_invalid",
    "environments.yaml",
    setitem(["resources"], {"ops_topic": {"type": "sns_topic", "params": {"colour": "blue"}}}),
)
simple(  # no store declared with the secret and none chosen by the environment
    "secret_provider_missing",
    "secret_provider_missing",
    "integrations.yaml",
    delitem(["secrets", "portal_client", "provider"]),
)

# ---------------------------------------------------------------- portal and destinations
simple("portal_required", "portal_required", "integrations.yaml", delitem(["portal"]))
simple(
    "destination_slot_unmapped",
    "destination_slot_unmapped",
    "integrations.yaml",
    lambda doc: doc["portal"]["endpoints"][0]["mapping"]["destinations"].pop("y"),
)
simple(
    "destination_slot_unbound",
    "destination_slot_unbound",
    "destinations.yaml",
    lambda doc: [e.update(slots=["x"]) for e in doc["destinations"]["dev"]],
)
simple(
    "destination_type_not_allowed",
    "destination_type_not_allowed",
    "integrations.yaml",
    lambda doc: doc["destinations"][1].update(allowed_types=["s3"]),
)


@case("destination_ambiguous", "destination_ambiguous")
def _ambiguous(d: Path) -> None:
    def mutate(doc: Any) -> None:
        e = copy.deepcopy(doc["destinations"]["dev"][0])
        e["id"] = "dev_delivery_s3_fixed"
        e["prefix_pattern"] = "studies/fixed/in"
        doc["destinations"]["dev"].append(e)

    edit(d, "destinations.yaml", mutate)


simple(
    "classification_target_mismatch",
    "classification_target_mismatch",
    "destinations.yaml",
    lambda doc: doc["destinations"]["dev"][0].update(max_classification="internal"),
)
simple(
    "destination_write_once_unsupported",
    "destination_write_once_unsupported",
    "destinations.yaml",
    lambda doc: doc["destinations"]["ppt"][2].update(method="POST"),
)

# ---------------------------------------------------------------- engines and workload
simple(
    "engine_not_allowed",
    "engine_not_allowed",
    "pipeline.yaml",
    lambda doc: task(doc, "t2_checksum").update(engine="fivetran_native"),
)
simple(
    "task_timeout_exceeds_engine",
    "task_timeout_exceeds_engine",
    "pipeline.yaml",
    lambda doc: task(doc, "t2_checksum").update(timeout="20m"),
)


@case("engine_infeasible", "engine_infeasible")
def _infeasible(d: Path) -> None:
    def mutate(doc: Any) -> None:
        tasks = doc["orchestration"]["tasks"]
        tasks.remove(task(doc, "t2_checksum"))
        t3 = task(doc, "t3_upload")
        t3.update(depends_on=["t1_collect"], engine="lambda", timeout="15m")
        t3["rules"].insert(0, "r_checksum")

    edit(d, "pipeline.yaml", mutate)


simple(
    "engine_marginal",
    "engine_marginal",
    "pipeline.yaml",
    lambda doc: task(doc, "t3_upload").update(engine="lambda", timeout="15m"),
)
simple(
    "workload_exceeds_limit",
    "workload_exceeds_limit",
    "pipeline.yaml",
    setitem(["workload", "bytes_per_run", "max"], "11GB"),
)

# ---------------------------------------------------------------- GxP, retention, checksums
simple(
    "gxp_spot", "gxp_spot_forbidden", "pipeline.yaml", lambda doc: task(doc, "t3_upload").update(engine="eks_pod_spot")
)
simple(
    "gxp_audit_missing",
    "gxp_audit_missing",
    "security.yaml",
    setitem(["security_and_access", "audit", "events"], ["delivery"]),
)
simple(
    "gxp_sha256_missing",
    "gxp_sha256_missing",
    "security.yaml",
    setitem(["security_and_access", "checksums", "manifest"], ["md5"]),
)
simple(
    "gxp_encryption_at_rest_missing",
    "gxp_encryption_at_rest_missing",
    "security.yaml",
    setitem(["security_and_access", "encryption_at_rest", "s3_destinations"], "sse_s3"),
)
simple(
    "gxp_external_destination_unattested",
    "gxp_external_destination_unattested",
    "destinations.yaml",
    lambda doc: doc["destinations"]["dev"][1].pop("at_rest_attestation"),
)
simple(
    "retention_shorter_than_replay",
    "retention_shorter_than_replay",
    "security.yaml",
    setitem(["security_and_access", "retention", "staging_after_failure"], "7d"),
)
simple(
    "checksum_policy_mismatch",
    "checksum_policy_mismatch",
    "rules.yaml",
    lambda doc: rule(doc, "r_upload_x")["implementation"]["params"].update(sidecars=["sha256"]),
)

# ---------------------------------------------------------------- environments and platform
simple("env_missing", "env_missing", "environments.yaml", delitem(["environments", "ppt"]))


@case("env_unknown", "env_unknown")
def _env_unknown(d: Path) -> None:
    edit(d, "destinations.yaml", lambda doc: doc["destinations"].update(qa=copy.deepcopy(doc["destinations"]["dev"])))


simple(
    "env_resource_shared",
    "env_resource_shared",
    "environments.yaml",
    setitem(["environments", "test", "queues", "package_ready", "name"], "clinops-package-ready-dev"),
)
simple(
    "unknown_placement_alias",
    "unknown_placement_alias",
    "environments.yaml",
    setitem(["environments", "dev", "placement", "workload"], "nowhere"),
)


@case("unknown_team", "unknown_team")
def _unknown_team(d: Path) -> None:
    toml = d.parent.parent / "factory.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace('"clinops_data"', '"other_team"'), encoding="utf-8")
    edit(d, "pipeline.yaml", setitem(["metadata", "team"], "other_team"))


@case("platform_settings_missing", "platform_settings_missing")
def _no_platform(d: Path) -> None:
    toml = d.parent.parent / "factory.toml"
    lines = [ln for ln in toml.read_text(encoding="utf-8").splitlines() if not ln.startswith("platform_settings")]
    toml.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- custom rules
CUSTOM_CODE = b"def run(ctx):\n    return True\n"


def _make_custom(d: Path, *, code: bytes | None, sha: str, test: bool) -> None:
    edit(
        d,
        "rules.yaml",
        lambda doc: rule(doc, "r_check_listing").update(
            implementation={"kind": "custom", "code_sha256": sha, "output_kinds": {}}
        ),
    )
    rd = d / "rules" / "r_check_listing"
    if code is not None:
        rd.mkdir(parents=True)
        (rd / "impl.py").write_bytes(code)
    if test:
        (rd / "test_impl.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")


CASES["custom_rule_code_missing"] = (
    "custom_rule_code_missing",
    lambda d: _make_custom(d, code=None, sha="0" * 64, test=False),
)
CASES["custom_rule_code_mismatch"] = (
    "custom_rule_code_mismatch",
    lambda d: _make_custom(d, code=CUSTOM_CODE, sha="0" * 64, test=True),
)
CASES["custom_rule_untested"] = (
    "custom_rule_untested",
    lambda d: _make_custom(d, code=b"x = 1\n", sha=hashlib.sha256(b"x = 1\n").hexdigest(), test=False),
)
CASES["custom_rule_unaccepted"] = (  # code, tests and owner cases present, but no person accepted it
    "custom_rule_unaccepted",
    lambda d: _make_custom(d, code=b"x = 1\n", sha=hashlib.sha256(b"x = 1\n").hexdigest(), test=True),
)
_SOCKET_CODE = b"import socket\n"
CASES["custom_rule_import_forbidden"] = (
    "custom_rule_import_forbidden",
    lambda d: _make_custom(d, code=_SOCKET_CODE, sha=hashlib.sha256(_SOCKET_CODE).hexdigest(), test=True),
)
simple(
    "glob_invalid",
    "glob_invalid",
    "rules.yaml",
    lambda doc: rule(doc, "r_list_objects")["implementation"]["params"].update(include=["data**x"]),
)

# codes whose cases live in other test modules
COVERED_ELSEWHERE = {
    "lock_missing": "test_spec_lock.py",
    "lock_stale": "test_spec_lock.py",
    "layout_unsupported": "factory/tests/test_dataops_layout.py",
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_invalid_case_raises_expected_code(name: str, pipeline_dir: Path) -> None:
    code, mutate = CASES[name]
    mutate(pipeline_dir)
    result = validate_dir(pipeline_dir)
    codes = {i.code for i in result.issues}
    assert code in codes, f"{name}: expected {code}, got {sorted(codes)}"
    found = next(i for i in result.issues if i.code == code)
    assert found.severity == CODES[code].severity
    assert found.hint
    assert found.hint == CODES[code].hint
    if CODES[code].severity == "error":
        assert not result.ok


def test_every_code_has_a_case() -> None:
    produced = {code for code, _ in CASES.values()} | set(COVERED_ELSEWHERE)
    assert sorted(set(CODES) - produced) == []


def test_every_code_has_a_plain_hint() -> None:
    for code, info in CODES.items():
        assert info.hint.endswith((".", ")")), code
        assert len(info.hint) >= 20, code


def test_issues_are_sorted_and_located(pipeline_dir: Path) -> None:
    CASES["op_unknown"][1](pipeline_dir)
    issues = validate_dir(pipeline_dir).issues
    assert issues == sorted(issues, key=lambda i: (i.file, i.loc, i.code, i.msg))
    op = next(i for i in issues if i.code == "op_unknown")
    assert op.file == "rules.yaml"
    assert op.loc == "rules[r_zip].implementation"


def test_unknown_field_suggests_spelling(pipeline_dir: Path) -> None:
    CASES["unknown_field"][1](pipeline_dir)
    i = next(i for i in validate_dir(pipeline_dir).issues if i.code == "unknown_field")
    assert i.loc == "metadata.nmae"
    assert "did you mean 'name'" in i.msg


def test_required_placeholder_hides_errors_below_it(pipeline_dir: Path) -> None:
    edit(pipeline_dir, "pipeline.yaml", setitem(["workload", "bytes_per_run"], "<REQUIRED>"))
    issues = validate_dir(pipeline_dir).issues
    assert [(i.code, i.loc) for i in issues if i.file == "pipeline.yaml"] == [("required", "workload.bytes_per_run")]


def test_secret_values_are_never_echoed(pipeline_dir: Path) -> None:
    CASES["secret_in_spec"][1](pipeline_dir)
    issues = validate_dir(pipeline_dir).issues
    hit = next(i for i in issues if i.code == "secret_in_spec")
    assert hit.loc.startswith("line ")
    assert all("AKIA" not in (i.msg + i.loc) for i in issues)


def test_secret_key_value_is_flagged_but_references_are_not(pipeline_dir: Path) -> None:
    f = pipeline_dir / "spec" / "integrations.yaml"
    text = f.read_text(encoding="utf-8")
    f.write_text(text + "# token: portal_client\n", encoding="utf-8")  # a declared secret name: a reference
    assert "secret_in_spec" not in {i.code for i in validate_dir(pipeline_dir).issues}
    f.write_text(text + "# password: Synthetic-Not-Real-1\n", encoding="utf-8")
    assert "secret_in_spec" in {i.code for i in validate_dir(pipeline_dir).issues}
