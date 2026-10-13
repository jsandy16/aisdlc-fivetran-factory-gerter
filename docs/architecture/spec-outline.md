# Spec outline (detailed models in Phase 2)

The spec is the single source of truth for a pipeline. It is YAML, backed by Pydantic v2 models (`extra="forbid"`),
exported as JSON Schema for editor autocomplete, and versioned with semver ([ADR-0003](../adr/0003-spec-layout.md)).

## 1. Physical layout: owner-scoped files, one model

| File | Sections | Owner (CODEOWNERS, exactly one) | BA touches |
|---|---|---|---|
| `spec/pipeline.yaml` | `metadata`, `trigger`, `orchestration`, `reliability`, `observability`, `workload`, `cost`, `lineage`, `documentation` | data engineer | metadata, schedule intent, workload hints |
| `spec/rules.yaml` | `rules[]` (with `test_cases`), `data_contract` | BA / product owner | `plain_english`, `test_cases`, naming tokens |
| `spec/integrations.yaml` | `portal`, `sources[]`, `targets[]`, `secrets` | data engineer | — |
| `spec/security.yaml` | `security_and_access` (gxp tier, classification, retention, KMS, residency, grants) | governance / QA | — |
| `spec/environments.yaml` | per-environment bindings: placement, portal `base_url`, buckets, Vault mounts and namespaces, queue ARNs, schedule overrides, enabled | platform | — |

- **One model.** The loader merges the files into one `PipelineSpec`. Every cross-file reference uses a **stable id**
  (rule_id, task_id, artifact name, secret name, source/target id), never a list index.
- **The lock.** `spec.lock.json` is the canonical, normalised merge: defaults filled in and short forms expanded.
  It is generated and committed.
- **Logic vs bindings.** Logic (everything except `environments.yaml`) hashes to `logic_sha`, which is the same in
  dev, test and prod. Each environment has a `binding_sha`. The pair is the GxP evidence that what was tested is
  what runs.
- **Derived, not authored.**
  - `infrastructure` comes from `platform-settings` plus the spec.
  - `artifacts` are inferred from rule inputs and outputs, and can be overridden.
  - `documentation` output is generated.
  - `changelog` is generated from `changes/`.
- **Gate.** `open_questions[]` lives in `pipeline.yaml` and must be empty before generation.

## 2. Sections and key fields

```yaml
# spec/pipeline.yaml
api_version: factory/v1
kind: Pipeline
schema_version: 1.0.0              # semver of the schema; migrations are chained (factory migrate)
metadata:
  pipeline_id: study_pkg_delivery  # = DAG id stem, role and namespace label; never changes
  name: "Study package delivery"
  team: clinops_data
  owners: {business_owner: "@alice", data_engineer: "@bob", governance: "@carol", quality: "@dan"}
  data_classification: restricted  # public < internal < confidential < restricted
  spec_version: 0.1.0              # semver of this pipeline
  requirements: [REQ-001]          # traceability ids
  tags: {cost_centre: CC1234}
trigger:                           # union: schedule | portal_schedule | s3_event | sqs | manual | upstream
  type: sqs
  queue: delivery_requests         # resolved per environment in environments.yaml
orchestration:
  dag: {catchup: false, max_active_runs: 4, dagrun_timeout_minutes: 240}
  pool: clinops_data
  tasks:
    - {task_id: t1_collect, rules: [r_read_request, r_portal_lookup, r_download, r_zip, r_manifest], engine: auto}
    - {task_id: t2_encrypt, rules: [r_encrypt], depends_on: [t1_collect], engine: auto}
    - {task_id: t3_checksum, rules: [r_md5], depends_on: [t2_encrypt], engine: auto}
    - {task_id: t4_upload, rules: [r_upload_x, r_upload_y], depends_on: [t3_checksum], engine: auto}
reliability:
  run_key: "{trigger.message.request_id}"    # deterministic per run
  retry: {max_attempts: 3, backoff: exponential_jitter, base_seconds: 10, max_seconds: 300}
  dlq: true
  replay_window_days: 30
observability:
  alerts: {transient: none, data: "sns:clinops-data", config: "sns:platform", security: "sns:security"}
  sla_minutes: 180
workload:                          # sizing hints for the engine planner and cost model
  bytes_per_run: {p50: 2GB, p95: 20GB, max: 50GB}
  files_per_run: {p50: 2000, max: 20000}
  runs_per_day: 4
cost: {budget_per_run_usd: 5, budget_per_month_usd: 600}
open_questions: []
```

```yaml
# spec/rules.yaml (one rule shown)
rules:
  - rule_id: r_encrypt
    category: encrypt              # discriminator → per-category model
    plain_english: "Read the encryption key from the vault entry and encrypt the package."
    implementation:                # union: builtin | expression | custom
      kind: builtin
      op: crypto.encrypt           # discriminator → per-op params model
      params: {format: aws_esdk, key_source: vault_kv, key_secret: package_key, frame_bytes: 1048576}
    inputs: [package]
    outputs: [package_encrypted]
    preconditions: ["artifacts.package.size > 0"]   # CEL
    on_failure: {action: fail, error_class: data}
    side_effects: []               # builtin ops declare theirs in the op catalog
    test_cases:
      - {name: encrypts_and_decrypts, given: {package: fixture://small.zip}, expect: {decrypts_to: fixture://small.zip}}
```

```yaml
# spec/integrations.yaml (abridged)
portal:
  auth: {type: oauth2_client_credentials, token_url_path: /oauth/token, client: portal_client}   # secret names
  endpoints:
    - id: study_paths
      method: GET
      path: /api/v1/studies/{study_id}/paths
      pagination: {type: cursor, cursor_field: next}
      response_schema: {items: "data[]", fields: {server_path: string, path_type: "enum[absolute,recursive]", include: "list[string]"}}
      response_mapping: {items: "data", path: "server_path", path_type: "path_type", include: "include", exclude: "exclude"}
  snapshot: per_run
  allowed_roots: ["/exports/studies/"]
sources:
  - {id: study_server, type: sftp, host_ref: study_sftp_host, host_key_ref: study_sftp_host_key, auth: {private_key: study_sftp_key}, path_from: portal.study_paths}
targets:
  - {id: x, type: s3, bucket_ref: delivery_bucket, prefix: "x/{study_id}/{run_date}/", kms_key_ref: delivery_kms, write_mode: write_once}
  - {id: y, type: s3, bucket_ref: archive_bucket, prefix: "y/{study_id}/", kms_key_ref: archive_kms, write_mode: write_once}
secrets:
  package_key: {provider: vault, path: "secret/data/{env}/clinops/package", key: aes256, kind: aes_key}
  portal_client: {provider: aws_secrets_manager, path: "{env}/clinops/portal-client", key: client_secret, kind: oauth_client}
  study_sftp_key: {provider: aws_secrets_manager, path: "{env}/clinops/sftp", key: private_key, kind: ssh_private_key}
```

## 3. Rules

- **Fields:** every rule has `rule_id`, `category`, `plain_english`, `implementation`, `inputs`, `outputs`,
  `preconditions`, `on_failure`, `side_effects` and `test_cases`.
- **Categories:** extract, transform, validate, package, encrypt, checksum, naming, upload, housekeeping,
  integration. The category is a discriminator: each category allows a fixed set of ops and its own fields.
- **Implementations:**
  - `builtin{op, params}`: an op from the [catalog](op-catalog.md), with params validated by the op's model;
  - `expression{cel}`: validation predicates and naming rules;
  - `custom{plugin, code_sha256}`: code under `rules/<rule_id>/`, reviewed, accepted and committed
    ([ADR-0020](../adr/0020-custom-rule-plugins.md)).
- **Composition:** tasks list rules in order. Tasks compose through `depends_on`, `branch` (a CEL condition) and
  `parallel_group`.

## 4. Cross-field validation (examples, Phase 2 builds the full set)

| Rule | Error |
|---|---|
| `encrypt` needs `key_secret` (vault/aws_sm) or `kms_key_ref` | `encrypt_without_key` |
| `trigger.type` `sqs` or `s3_event` needs a queue binding in every enabled environment | `event_trigger_without_queue` |
| Recursive discovery needs at least one `include` pattern | `recursive_without_include` |
| `gxp_tier ≠ non_gxp` needs encryption on every target, an audit trail, sha256 in the manifest, and on-demand engines only | `gxp_controls_missing` |
| An explicit `engine: lambda` must pass the feasibility check against `workload.max` | `engine_infeasible` |
| Every secret, artifact, rule and source/target reference resolves; every input has an upstream producer; cross-task artefacts are staged | `unresolved_reference`, `dataflow_gap` |
| Portal-returned paths are constrained by `allowed_roots`; source hosts come from the spec | `portal_unconstrained` |
| A classification above the target's allowed level | `classification_target_mismatch` |
| A `custom` rule needs `test_cases`, a review record and an acceptance digest matching `code_sha256` | `custom_rule_unreviewed` |

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

| Starter | Trigger | Engines |
|---|---|---|
| `portal_zip_encrypt_s3` (the worked example) | sqs / portal_schedule | eks_pod (collect, encrypt), lambda (checksum, copy) |
| `s3_to_s3_event` | s3_event → Lambda | lambda, or eks_pod for large objects |
| `table_to_s3_fivetran` | schedule | fivetran_native, or fivetran_sdk when row rules are needed |
| `rest_to_s3` | schedule | lambda or eks_pod by volume |

## 8. Worked example outline (the full spec is built in Phase 2)

> "Read the server path from the self-service portal and download all objects, then package them. Create a DAG with
> 4 sequential tasks: Task 1 reads the SQS queue, downloads all objects and prepares a zip. Task 2 reads the
> encryption key from the vault entry and encrypts the package. Task 3 creates an MD5 checksum. Task 4 uploads to
> the S3 bucket at the `<x>` and `<y>` prefixes."

| Task | Rule | Category | Implementation | Output |
|---|---|---|---|---|
| t1_collect | r_read_request | integration | `sqs.read` (dispatch mode: reads the custody snapshot) | `message_batch` |
| | r_portal_lookup | integration | `portal.lookup` (endpoint `study_paths`, snapshot) | `portal_snapshot` |
| | r_list | extract | `discover.list` (path from snapshot, recursive, include/exclude, `allowed_roots`) | `file_set` (listing) |
| | r_download | extract | `transfer.download` (streaming) | `file_set` |
| | r_zip | package | `package.zip` (preserve structure under the portal path, deterministic) | `package` (staged) |
| | r_manifest | package | `manifest.build` (path, size, sha256 per file) | `manifest` (staged) |
| t2_encrypt | r_encrypt | encrypt | `crypto.encrypt` (`aws_esdk`, `key_source: vault_kv` per the brief; KMS recommended, Q2) | `package_encrypted` (staged) |
| t3_checksum | r_md5 | checksum | `checksum.compute` (md5 of the stored encrypted object; sha256 too) | `checksum` + sidecar |
| t4_upload | r_name | naming | `naming.render` (`{study_id}_{run_date}_{seq}.zip.enc`) | `value` |
| | r_upload_x | upload | `s3.copy` → target `x` (write-once, SSE-KMS, tags) | final object + sidecar |
| | r_upload_y | upload | `s3.copy` → target `y` | final object + sidecar |

Trigger: `sqs` in `dispatch` mode ([ADR-0010](../adr/0010-event-triggers-and-sqs-custody.md)). Engines per the
planner ([engine-selection.md §3](engine-selection.md)).
