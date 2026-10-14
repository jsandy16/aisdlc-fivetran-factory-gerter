"""Every issue code the spec loader and validators can emit, with a plain-English hint (tests check coverage)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from factory_core.envelope import Issue


@dataclass(frozen=True)
class CodeInfo:
    severity: Literal["error", "warning"]
    hint: str


CODES: dict[str, CodeInfo] = {
    # load
    "missing_file": CodeInfo("error", "Create the file; `factory new` writes all six spec files."),
    "yaml_syntax": CodeInfo("error", "Fix the YAML syntax at the reported line (indentation, quotes, colons)."),
    "yaml_alias": CodeInfo("error", "Write the value out in full; YAML anchors and aliases are not allowed."),
    "yaml_tag": CodeInfo("error", "Remove the '!' tag; spec values are plain text, numbers, lists and mappings."),
    "yaml_duplicate_key": CodeInfo("error", "Remove one of the two keys with the same name."),
    "yaml_date": CodeInfo("error", 'Put dates in quotes, e.g. "2024-03-01".'),
    "yaml_key_type": CodeInfo("error", "Quote the key so it is text."),
    "yaml_too_large": CodeInfo("error", "Split the content; spec files are limited to 1 MiB."),
    "required": CodeInfo("error", "Fill in the value (it is still <REQUIRED>)."),
    "required_field": CodeInfo("error", "Add the missing field."),
    "unknown_field": CodeInfo("error", "Remove the field or fix its spelling."),
    "invalid_choice": CodeInfo("error", "Use one of the allowed values listed in the message."),
    "invalid_value": CodeInfo("error", "Correct the value as described in the message."),
    "template_unquoted": CodeInfo("error", 'Quote templates in YAML, e.g. run_key: "{request_id}".'),
    "quote_value": CodeInfo("error", 'Quote the value so it is text (e.g. "001234").'),
    "api_version_mismatch": CodeInfo("error", "Use api_version: factory/v1."),
    "schema_version_unsupported": CodeInfo(
        "error", "Use a schema_version this factory supports, or upgrade the factory."
    ),
    "schema_version_outdated": CodeInfo(
        "warning", "Run `factory migrate` to move the spec to the current schema version."
    ),
    "pipeline_id_folder_mismatch": CodeInfo("error", "Rename the folder or the pipeline_id so they match."),
    "open_questions_pending": CodeInfo("error", "Answer every open question, then remove it from open_questions."),
    "team_mismatch": CodeInfo("error", "metadata.team must equal the team in factory.toml."),
    # ops
    "op_unknown": CodeInfo("error", "Use an op from the catalog for this category (`factory schema` lists them)."),
    "op_category_mismatch": CodeInfo("error", "Move the rule to the category the op belongs to, or pick another op."),
    "op_not_available": CodeInfo("error", "This op is planned but not available yet; use another op or a custom rule."),
    # references
    "duplicate_id": CodeInfo("error", "Give every item a unique id."),
    "unresolved_reference": CodeInfo("error", "Point to an id that exists (check spelling and the file it lives in)."),
    "rule_unassigned": CodeInfo("error", "Add the rule to a task in orchestration.tasks, or delete it."),
    "rule_assigned_twice": CodeInfo("error", "A rule runs in exactly one task; remove it from the other task."),
    "task_cycle": CodeInfo("error", "Remove the circular depends_on between tasks."),
    "secret_kind_mismatch": CodeInfo("error", "Use a secret of the kind this field needs (see the message)."),
    "secret_unused": CodeInfo("warning", "Remove the unused secret reference."),
    "resource_invalid": CodeInfo(
        "error", "Use a type from the resource catalog with its parameters (factory_spec/catalog/resources.v1.yaml)."
    ),
    "secret_provider_missing": CodeInfo(
        "error", "Say where the secret is read from: environments.yaml → secrets.default or secrets.overrides."
    ),
    "secret_in_spec": CodeInfo(
        "error", "Remove the secret value; specs hold secret references only (secrets section)."
    ),
    "variable_unresolvable": CodeInfo("error", "Point the variable at an existing trigger field or portal value."),
    # dataflow
    "dataflow_gap": CodeInfo("error", "Produce the artifact in an earlier rule, or fix the input name."),
    "dataflow_multiple_producers": CodeInfo("error", "Give each output a unique artifact name."),
    "dataflow_order": CodeInfo("error", "Move the producing rule before the consuming rule (same or upstream task)."),
    "artifact_kind_mismatch": CodeInfo("error", "Use an input of the kind the op expects (see the message)."),
    "output_arity_mismatch": CodeInfo("error", "List exactly as many outputs as the op produces."),
    "dataflow_not_staged": CodeInfo("error", "Streams cannot cross tasks; consume the stream in the same task."),
    "artifact_unused": CodeInfo("warning", "Remove the output, or use it in a later rule."),
    # expressions and templates
    "template_syntax": CodeInfo("error", "Fix the template: tokens look like {name}; write {{ and }} for braces."),
    "template_unknown_token": CodeInfo(
        "error", "Use a declared variable (pipeline.yaml → variables) or a built-in token."
    ),
    "template_token_not_allowed": CodeInfo(
        "error", "This token is not allowed here (see the message for allowed tokens)."
    ),
    "template_token_unconstrained": CodeInfo(
        "error", "Give the trigger field a 'pattern' so the value is a safe path segment."
    ),
    "run_key_invalid": CodeInfo("error", "Build the run key only from trigger fields (variables from trigger.*)."),
    "cel_invalid": CodeInfo(
        "error", "Fix the CEL expression syntax (e.g. artifacts.listing.count == vars.expected_files)."
    ),
    "cel_unknown_name": CodeInfo("error", "Use artifacts.<input>, vars.<variable> or run.<field> in expressions."),
    "jmespath_invalid": CodeInfo("error", "Fix the JMESPath expression, e.g. targets[?slot=='x'] | [0].uri."),
    # triggers and sources
    "event_trigger_without_queue": CodeInfo(
        "error", "Bind the trigger's queue in environments.yaml for every environment."
    ),
    "trigger_field_missing": CodeInfo("error", "Declare the field in the trigger message as a required string."),
    "recursive_without_include": CodeInfo("error", 'Add at least one include pattern, e.g. ["**/*"].'),
    "glob_invalid": CodeInfo(
        "error", "Fix the pattern: *, ?, [abc] and whole-segment ** only, relative, no backslash."
    ),
    "source_unconstrained": CodeInfo(
        "error", "List the allowed buckets and prefix roots for this source in every environment."
    ),
    # portal and destinations
    "portal_required": CodeInfo(
        "error", "Add the portal section to integrations.yaml (and its base_url per environment)."
    ),
    "destination_slot_unmapped": CodeInfo("error", "Map the slot in the portal endpoint's mapping.destinations."),
    "destination_slot_unbound": CodeInfo(
        "error", "Add an allow-list entry for the slot in destinations.yaml for every environment."
    ),
    "destination_type_not_allowed": CodeInfo("error", "Use an entry type the slot allows, or extend allowed_types."),
    "destination_pattern_too_broad": CodeInfo("error", "Start the pattern with a fixed folder name."),
    "destination_pattern_invalid": CodeInfo("error", "Fix the pattern: literal segments, {var}, * or a trailing **."),
    "destination_ambiguous": CodeInfo(
        "error", "Make the overlapping allow-list entries disjoint (different hosts or folders)."
    ),
    "classification_target_mismatch": CodeInfo("error", "Use a destination approved for this data classification."),
    "destination_write_once_unsupported": CodeInfo(
        "error", "Use method PUT with write_once: if_none_match, or set write_once: none for POST endpoints."
    ),
    # engines and workload
    "engine_not_allowed": CodeInfo("error", "Pick an engine every op in the task supports (or engine: auto)."),
    "task_timeout_exceeds_engine": CodeInfo(
        "error", "Lower the timeout or use another engine (Lambda allows 15 minutes)."
    ),
    "engine_infeasible": CodeInfo(
        "error", "Use engine: auto or eks_pod; the task cannot finish on this engine at the maximum size."
    ),
    "engine_marginal": CodeInfo(
        "warning", "The task fits only without the safety margin; consider engine: auto or eks_pod."
    ),
    "workload_exceeds_limit": CodeInfo(
        "error", "The platform limit is 10GB per run; lower workload.bytes_per_run.max."
    ),
    # GxP
    "gxp_spot_forbidden": CodeInfo("error", "GxP pipelines run on on-demand capacity; use eks_pod, not eks_pod_spot."),
    "gxp_audit_missing": CodeInfo("error", "Enable the audit trail with delivery and destination_resolution events."),
    "gxp_sha256_missing": CodeInfo("error", "Add sha256 to the manifest checksums and build a manifest."),
    "gxp_encryption_at_rest_missing": CodeInfo("error", "Use sse_kms for S3 destinations."),
    "gxp_external_destination_unattested": CodeInfo(
        "error", "Record the at-rest attestation id for this external destination."
    ),
    "retention_shorter_than_replay": CodeInfo(
        "error", "Keep staging after failure at least as long as the replay window."
    ),
    "checksum_policy_mismatch": CodeInfo("error", "Deliver the checksum files security.yaml requires."),
    # environments and platform
    "env_missing": CodeInfo("error", "Add bindings for every environment listed in factory.toml."),
    "env_unknown": CodeInfo("error", "Remove the environment or add it to factory.toml."),
    "binding_missing": CodeInfo("error", "Add the binding for this environment (see the message)."),
    "layout_unsupported": CodeInfo("error", "Change the spec as the message says; this layout cannot run it."),
    "env_resource_shared": CodeInfo("error", "Use a different queue or bucket per environment."),
    "unknown_placement_alias": CodeInfo("error", "Use a placement alias defined in the platform settings."),
    "unknown_team": CodeInfo("error", "The team must be defined in the platform settings of every environment."),
    "platform_settings_missing": CodeInfo(
        "warning", "Point factory.toml → platform_settings at the platform settings to check placements."
    ),
    # custom rules
    "custom_rule_code_missing": CodeInfo(
        "error", "Add rules/<rule_id>/impl.py (the rule-implementation agent writes it)."
    ),
    "custom_rule_code_mismatch": CodeInfo(
        "error", "Update code_sha256 to the sha256 of rules/<rule_id>/impl.py after review."
    ),
    "custom_rule_untested": CodeInfo("error", "Add rules/<rule_id>/test_impl.py and owner test_cases."),
    "custom_rule_unaccepted": CodeInfo(
        "error", "Run the gates and the review, then a person runs `factory accept <pipeline> <rule_id> --by <name>`."
    ),
    "custom_rule_import_forbidden": CodeInfo(
        "error", "Use only the plugin API and the allowed standard modules; no I/O, network or dynamic code."
    ),
    # lock
    "lock_missing": CodeInfo("error", "Run `factory lock <pipeline>` and commit spec.lock.json."),
    "lock_stale": CodeInfo("error", "Run `factory lock <pipeline>`; the spec changed since the lock was written."),
}


def issue(code: str, file: str, loc: str, msg: str, *, severity: Literal["error", "warning"] | None = None) -> Issue:
    info = CODES[code]
    return Issue(severity=severity or info.severity, file=file, loc=loc, code=code, msg=msg, hint=info.hint)
