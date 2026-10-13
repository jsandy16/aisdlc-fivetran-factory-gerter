# Spec outline (detailed models in Phase 2)

The spec is the single source of truth for a pipeline. It is YAML, backed by Pydantic v2 models (`extra="forbid"`),
exported as JSON Schema for editor autocomplete, and versioned with semver ([ADR-0003](../adr/0003-spec-layout.md)).

## 1. Physical layout: owner-scoped files, one model

| File | Sections | Owner (CODEOWNERS, exactly one) | Hash | BA touches |
|---|---|---|---|---|
| `spec/pipeline.yaml` | `metadata`, `variables`, `trigger`, `orchestration`, `reliability`, `observability`, `workload`, `cost`, `artifacts` (overrides), `open_questions` | data engineer | logic | metadata, schedule intent, workload hints |
| `spec/rules.yaml` | `rules[]` (with `test_cases`) | BA / product owner | logic | `plain_english`, `test_cases`, naming |
| `spec/integrations.yaml` | `sources[]`, `portal`, `apis[]`, `destinations[]` (slots), `secrets` | data engineer | logic | — |
| `spec/security.yaml` | `security_and_access`: gxp tier, **data classification**, at-rest encryption, checksums, audit, retention, residency | governance / QA | logic | — |
| `spec/environments.yaml` | per environment: placement, queues, source bucket allow-list, portal `base_url`, alert channels, secret mounts | platform | binding | — |
| `spec/destinations.yaml` | per environment: the **destination allow-list** the portal may choose from ([ADR-0024](../adr/0024-portal-resolved-destinations.md)) | governance | binding | — |

- **One model.** The loader merges the six files into one `PipelineSpec`. Every cross-file reference uses a **stable
  id** (rule_id, task_id, artifact, variable, secret, source, slot, endpoint, queue, allow-list entry id).
- **Environments** come from the team repo's `factory.toml` (`environments = ["dev","test","ppt","prod"]`), and
  every one must be bound.
- **The lock.** `spec.lock.json` is the canonical, normalised merge: defaults filled in, set-like lists sorted,
  id-keyed lists sorted by id. It is generated and committed (`factory lock`, `factory lock --check` in CI).
- **Logic vs bindings.**
  - `logic_sha` covers `pipeline`, `rules`, `integrations` and `security`, excluding cosmetic metadata (name,
    description, owners, tags, labels, spec_version). It is the same in dev, test, ppt and prod.
  - `binding_sha[env]` covers that environment's slice of `environments.yaml` and `destinations.yaml`.
- **Gate.** `open_questions[]` must be empty before generation.

## 2. Key concepts

- **`variables`:** the only names that templates, path patterns and CEL `vars` may use, e.g.
  `study_id: trigger.message.study_id`. A variable used in a path must come from a field with a `pattern`.
- **Trigger:** a union of `schedule | sqs | s3_event | manual`. `portal_schedule` and `upstream` come in a later
  minor version.
  - The SQS trigger declares its `message` fields (type, pattern, bounds) and `mode: dispatch | in_task`.
  - `reliability.run_key` may use only trigger-sourced variables.
- **Portal:** auth, endpoints, pagination, `response` field specs (→ generated model), JMESPath `mapping`
  (destinations per slot, values, `expect` guards) ([portal-integration.md](portal-integration.md)).
- **Destinations:**
  - slots in `integrations.yaml` resolve via the portal or statically;
  - `destinations.yaml` lists the allowed `s3` / `sftp` / `https` entries per environment;
  - credentials, KMS keys and egress come from the matched entry.
- **Units:** sizes like `10GB` / `512MiB` (case-sensitive, integer bytes), durations like `15m` / `4h` (integer
  seconds), money as `"2.00"`. No floats in hashed data.

The complete worked example lives in
`examples/team-pipelines/pipelines/study_pkg_delivery/spec/` (§8) and is validated in CI.

## 3. Rules

- **Fields:** every rule has `rule_id`, `category`, `plain_english`, `implementation`, `inputs`, `outputs`,
  `preconditions`, `on_failure`, `side_effects` and `test_cases`.
- **Categories:** extract, transform, validate, package, encrypt (*planned*: package encryption is deferred, D3),
  checksum, naming, upload, housekeeping, integration. The category is a discriminator: each category allows a fixed set of ops and its own fields.
- **Implementations:**
  - `builtin{op, params}`: an op from the [catalog](op-catalog.md), with params validated by the op's model;
  - `expression{cel}`: validation predicates and naming rules;
  - `custom{plugin, code_sha256}`: code under `rules/<rule_id>/`, reviewed, accepted and committed
    ([ADR-0020](../adr/0020-custom-rule-plugins.md)).
- **Composition:** tasks list rules in order. Tasks compose through `depends_on`, `branch` (a CEL condition) and
  `parallel_group`.

## 4. Cross-field validation (selection; the full list with codes is in [phase-2-design.md](phase-2-design.md) §D)

| Rule | Error code |
|---|---|
| An `encrypt` rule or `crypto.*` op is used while package encryption is deferred | `op_not_available` |
| `trigger.type` `sqs` or `s3_event` needs a queue binding in every environment | `event_trigger_without_queue` |
| Recursive listing (`s3.list` / `discover.list` with `recursive: true`) needs at least one `include` pattern | `recursive_without_include` |
| `gxp_tier ≠ non_gxp` needs SSE-KMS at rest, an audit trail, sha256 in the manifest, no spot, and an at-rest attestation on external destinations | `gxp_encryption_at_rest_missing`, `gxp_audit_missing`, `gxp_sha256_missing`, `gxp_spot_forbidden`, `gxp_external_destination_unattested` |
| An explicit `engine: lambda` must pass the feasibility check against `workload.max`, using the union of all environments' destination types | `engine_infeasible` (error), `engine_marginal` (warning) |
| Every secret, artifact, rule, variable, slot and endpoint reference resolves; every input has exactly one upstream producer; cross-task artefacts are staged | `unresolved_reference`, `dataflow_gap`, `dataflow_not_staged` |
| Every portal-resolved slot has at least one allow-list entry per environment; patterns are not too broad and do not overlap | `destination_slot_unbound`, `destination_pattern_too_broad`, `destination_ambiguous` |
| A secret value pasted into a spec | `secret_in_spec` |
| Package size limit (10 GB) | `workload_exceeds_limit` |

## 5. BA-friendliness

- `factory show <p> --view ba` renders the merged spec as numbered sections with plain-English labels
  (`x-ba-label` in the JSON Schema).
- The Intake agent asks about one section at a time and writes typed patches. BAs never hand-edit YAML unless they
  want to.
- Short forms are accepted and expanded in the lock: `checksum: md5`, `schedule: daily 02:00`.
- Every error names the file and path, says what is wrong in plain English and suggests a fix.

## 6. Versioning and migration

- `schema_version` (semver):
  - patch = documentation only;
  - minor = additive with defaults;
  - major = needs a migration.
- Migrations are pure functions `v_n → v_n+1`, chained by `factory migrate`, each with golden tests.
- `spec_version` is the pipeline's own semver. A breaking change to DAG shape bumps its major, which becomes a new
  `dag_id` ([ADR-0018](../adr/0018-change-engine.md)).

## 7. Starter templates (Phase 2)

| Starter | Trigger | Shape |
|---|---|---|
| `s3_package_deliver` (the worked example) | sqs (S3 package metadata) | collect + zip → checksum → deliver to portal-resolved destinations |
| `s3_to_s3_event` | s3_event | server-side copy to a static destination slot |
| `rest_to_s3` | schedule | `rest.fetch` (paginated API) → `s3.put` to a static slot |
| `table_to_s3_fivetran` | schedule | `fivetran.sync` on `fivetran_native` (model only; REST provisioning in Phase 5) |

## 8. Worked example (complete spec in `examples/team-pipelines/pipelines/study_pkg_delivery/spec/`)

The requirement, as clarified by the user (D1–D3):

> A package is uploaded to S3, and an SQS message carries its metadata (bucket, prefix, …). Download all objects
> under that prefix and zip them, keeping the folder structure. Create an MD5 checksum. Upload to destinations
> `<x>` and `<y>`, which the self-service portal names (another S3 bucket/prefix or an external server). Encryption
> is done outside the pipeline for now.

| Task | Rule | Category | Implementation | Output |
|---|---|---|---|---|
| t1_collect | r_read_request | integration | `sqs.read` (custody snapshot of the message) | `request` (message) |
| | r_portal_lookup | integration | `portal.lookup` (`delivery_targets`, with the study_id `expect` guard) | `portal_answer` (snapshot) |
| | r_resolve_destinations | integration | `destination.resolve` (slots x, y against the allow-list) | `destinations` |
| | r_list_objects | extract | `s3.list` (bucket/prefix from the message, recursive, include `**/*`, ≤ 20k objects, ≤ 10GB) | `listing` |
| | r_check_listing | validate | CEL: count and bytes equal the message | — |
| | r_download | extract | `s3.download` (streaming, size + ETag verified) | `files` |
| | r_zip | package | `package.zip` (deterministic, paths relative to the prefix) | `package` (staged) |
| | r_manifest | package | `manifest.build` (per-file sha256; zip md5 + sha256) | `manifest` (staged) |
| t2_checksum | r_checksum | checksum | `checksum.compute` (re-read the stored zip; md5 + sha256 equal the manifest) | `package_checksum` |
| t3_upload | r_package_name | naming | `naming.render` (`{study_id}_{package_id}.zip`) | `package_name` |
| | r_upload_x | upload | `destination.deliver` (slot x; write-once; manifest + md5/sha256 sidecars; verify) | receipt |
| | r_upload_y | upload | `destination.deliver` (slot y) | receipt |

Trigger: `sqs` in `dispatch` mode ([ADR-0010](../adr/0010-event-triggers-and-sqs-custody.md)). Engines per the
planner ([engine-selection.md §3](engine-selection.md)).
