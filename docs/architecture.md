# Architecture

## Specs (owned by people)

| File | Owner | Holds |
|---|---|---|
| `pipeline.yaml` | product owner | name, version, GxP tier, owners, requirement, environments, schedule, runtime |
| `source.yaml` | data engineer | connection (secrets only as `{secret: key}`), delivery (package) matching, readiness, package transforms and structural checks |
| `destination.yaml` | data engineer (platform) | Managed Data Lake per environment: Fivetran group, bucket, prefix, region, role, Glue |
| `sttm.yaml` | data engineer + business | entities, attributes, types, transforms and checks (attribute, row, entity) |
| `quality.yaml` | quality owner | post-load lake checks (SELECT-only SQL), severity, ticket route |
| `policy.yaml` | governance | classification, access grants, retention, audit and originals buckets |

Every model forbids unknown keys. Placeholders (`<REQUIRED>`) are reported, never defaulted. Cross-validation
covers names (Fivetran naming), references, CEL syntax and names, secrets, TLS policy, GxP rules. CODEOWNERS
routes each file to its owner.

## Checks — where each runs

| Scope | Examples | Runs | On failure |
|---|---|---|---|
| package (delivery) | name, size, checksum sidecar, type, folder, members, duplicates, custom CEL/English | connector, before load | reject the delivery |
| entity | row count, uniqueness, control-file count | connector, before emit | reject delivery / entity |
| row & attribute | not null, regex, range, allowed values, length, CEL, English, python | connector, per row | quarantine row (threshold → reject) |
| lake | row count, unique, freshness, referential, reconciliation, SQL | `pf dq run` (Athena) after sync | ticket; blockers withhold certification |

Every check has `mode: shadow|enforce`; new rules on a deployed pipeline start in shadow. Results land in the
evidence tables `pf_package_audit`, `pf_entity_audit`, `pf_rule_results`, `pf_quarantine` (never data values).

## Connector runtime

* Discovery lists the source, matches deliveries, applies readiness, orders by named groups, and audits files
  that match nothing (`on_unmatched: warn`).
* Deliveries are prepared in parallel (download, hash, archive the original write-once, checks, unpack, parse,
  rules, spool) and emitted strictly in order on the main thread; state is checkpointed after each delivery.
* Idempotent: state keeps each delivery's sha256 and a fingerprint including its checksum sidecars, so a
  rejected delivery is retried when the vendor fixes the sidecar; a loaded one re-sent with new content follows
  `on_redelivery`.
* Database sources read with a read-only session, a server-side cursor and a `>=` cursor column, checkpointing
  per batch.
* Transient errors retry with backoff; exhausted retries fail the sync cleanly (`op.error`); config errors stop
  before any data.
* TLS always verifies: SFTP pins the host key; FTPS and RDS `verify-full` verify certificates (optional pinned CA).
* Lineage columns on every row: `pf_package`, `pf_package_hash`, `pf_member`, `pf_row`, `pf_run_id`,
  `pf_pipeline_version`. Column names avoid letter/digit boundaries, which Fivetran's naming would rewrite.

## Outside Fivetran (generated from the specs)

* Lake Formation LF-tags (`pf_classification`, `pf_certified`, `pf_pipeline`) and tag-based grants: consumers see
  certified tables only.
* Audit bucket with Object Lock and a CloudTrail data-event trail; originals bucket with Object Lock.
* ODCS v3.2.0 data contract, README with lineage, per-environment Fivetran settings.

## Agents, skills, determinism

Interactive work goes through the skills (`creating-pipeline-specs`, `changing-pipeline-rules`,
`building-and-releasing-pipelines`, `extending-pipeline-factory`) which call `pf --json`. Headless LLM steps
run through the Claude Agent SDK with exactly one plugin skill, only the `Skill` tool, no settings sources,
`permission_mode="dontAsk"`, a PreToolUse hook that denies every other tool, `output_format` JSON Schema
generated from pydantic (strict: all fields required, no extra keys), turn and budget limits, and a check that
the skill was actually loaded. Answers are validated again by pydantic, logged to `llm-calls.jsonl` with hashes
of prompt, schema and skill, and recorded for replay.

Hooks deny agents edits to generated files, `rules.lock.yaml`, release records and workflows; deny deploy,
accept and secret access; and re-validate specs after every edit. The real boundary is that deploy credentials
exist only in CI.

## Deployment (Git)

Pull request → `verify` (validate, `build --check`, `test`, `release --check`). Merge to main → per
environment, through a GitHub Environment (required reviewers on prod): upload the release artefact write-once,
then `pf deploy`: copy the build, prove it equals the committed release record, run the official
`fivetran deploy --yes` with the configuration assembled from environment secrets (API key via
`FIVETRAN_API_KEY`), fail if the SDK altered `requirements.txt`, then apply the schedule through the REST API.

Requirements are generated, pinned and written in the normalised form the SDK compares; `pf build` checks every
import offline. `celpy` is imported through importlib because the cel-python wheel has no `top_level.txt`: the
SDK's pipreqs step would otherwise resolve it to the unrelated PyPI package `CelPy` and `--yes` would add it.
