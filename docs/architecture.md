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
* Idempotent: state keeps, per path, what was loaded there (never overwritten by a later rejection) and a
  fingerprint of the last version including its checksum sidecars, so a rejected delivery is retried when the
  vendor fixes the sidecar; a loaded one re-sent with new content follows `on_redelivery`.
  `change_detection: content` also catches re-uploads that keep size and modification time.
* Ordering across syncs follows `late_data_policy` (default `wait`): newer deliveries wait while an older one is
  not ready, and a delivery older than one already loaded is rejected as late (audited), so an older file can
  never overwrite newer upserted rows. `load_late` and `reject_late` are explicit alternatives.
* Downloads are verified against the listing size; archives that are encrypted, corrupt or bomb-like reject only
  that delivery. Files matching no pattern, late files and future-dated files are audited, never silently skipped.
* Historical re-sync (Fivetran passes empty state): the loaded-delivery registry is rebuilt from write-once markers
  in the originals bucket, so redelivery and duplicate rules still hold; `pf_full_reload=true` reloads on purpose.
* Database sources read with a read-only session, a server-side cursor and a `>=` cursor column (the cursor
  must be NOT NULL). Incremental tables checkpoint as they go; a full reload commits only after the whole table
  was read and checked, so a failure never leaves a truncated partial snapshot. Incremental reads cannot see hard
  deletes at the source (open decision: periodic full reload or key reconciliation).
* Checkpoints are coalesced to at most one a minute (Fivetran guidance); every row key is deterministic, so a crash
  re-emits idempotently.
* Transient errors retry with backoff; exhausted retries fail the sync cleanly (`op.error`); config errors stop
  before any data.
* TLS always verifies: SFTP pins the host key; FTPS and RDS `verify-full` verify certificates (optional pinned CA).
* Lineage columns on every row: `pf_package`, `pf_package_hash`, `pf_member`, `pf_row`, `pf_run_id`,
  `pf_pipeline_version`, `pf_key_hash`; upsert entities also keep a key registry `pf_keys_<entity>` so the lake
  reconciliation finds lost rows as well as orphans. Names that Fivetran's naming would rewrite (letter/digit
  boundaries, `__`) are rejected at validation.
* For file sources each delivery's evidence is also written once to the originals bucket (`_evidence/`), because
  the lake's `pf_*` tables are Iceberg tables Fivetran maintains and expires.

## Outside Fivetran (generated from the specs)

* `foundation/<env>/` (repository level, aggregated over all pipelines; apply first): LF-tags `pf_classification`,
  `pf_certified`, `pf_pipeline`; lake data locations registered with Lake Formation (no hybrid IAM access) and
  Fivetran's data-location access; write-once audit and originals buckets (Object Lock, KMS, TLS-only); CloudTrail
  with log-file validation, lake data events and management events (grants, tag changes).
* Per pipeline `terraform/<env>/`: the Glue database (created before the first sync, uncertified), Fivetran's
  database permissions, consumers' tag-based grants (certified tables only), the DQ runner's grants, a
  least-privilege policy for the originals credentials; `columns/` tags classified columns after the first sync.
* `pf dq run --certify` certifies only when no blocker failed, every classified column carries its tag and Lake
  Formation (not IAMAllowedPrincipals) governs access; evidence (who, spec and checks hashes, query ids, Iceberg
  snapshot ids) is written once before the tag changes. `pf dq decertify` is for the sync-start webhook.
* ODCS v3.2.0 data contract (validated against the official schema), README with lineage, Fivetran settings.

## Agents, skills, determinism

Interactive work goes through the `plugin/` skills (`creating-pipeline-specs`, `changing-pipeline-rules`,
`building-and-releasing-pipelines`, `extending-pipeline-factory`), which call `pf --json`. The three LLM steps live
in a separate `steps-plugin/` that only `pf` loads through the Claude Agent SDK: one skill per session, only the
`Skill` and `StructuredOutput` tools (MCP servers, subagents and settings excluded and checked at start-up),
`permission_mode="dontAsk"`, a PreToolUse hook denying anything else, a pinned model, turn/budget/time limits,
`output_format` JSON Schema generated from pydantic, and a check that the skill call actually succeeded. Answers
are validated again by pydantic and logged; recordings carry their provenance and an output hash, and the replay
key includes the skill's content and the model, so a changed skill can never replay a stale answer.

English rules: owners label at least 3 examples (a pass and a fail); the compiled CEL must reproduce them and
the model's boundary cases, and deterministic mutation testing (flipped comparison operators, dropped null
allowances) must be killed by the cases. Acceptance records a digest of text, expression and cases plus the
acceptor's identity; any later edit invalidates it, and every build re-verifies with the runtime engine.

Guard rails: CODEOWNERS lists exactly one owner per governed file (policy, rules lock, quality, pipeline); the
plugin hook tokenises shell commands and denies person-only commands, writes to generated or person-owned files
and changes to the agent-session marker, failing closed; project settings deny the same. The boundaries are
CI-only deploy credentials and owner review of the acceptance in the pull request.

## Deployment (Git)

Pull request → `verify` (validate, `build --check`, `test`, `release --check`). Merge to main → per
environment, through a GitHub Environment (required reviewers on prod): upload the release artefact write-once,
then `pf deploy`: copy the build, prove it equals the committed release record, run the official
`fivetran deploy --yes` with the configuration assembled from environment secrets (API key via
`FIVETRAN_API_KEY`), fail if the SDK altered `requirements.txt`, then apply the schedule through the REST API.

Requirements are generated, pinned and written in the normalised form the SDK compares; `pf build` checks every
import offline. `celpy` is imported through importlib because the cel-python wheel has no `top_level.txt`: the
SDK's pipreqs step would otherwise resolve it to the unrelated PyPI package `CelPy` and `--yes` would add it.
