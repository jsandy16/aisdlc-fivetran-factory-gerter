# pipeline-factory — how it runs end to end, and an end-to-end run

Framework version 0.1.0 (from `pipeline-factory.zip`), read against the code and the manual
(`pipeline-factory-manual.zip`). Run evidence: [`dryrun-2026-10-13/DRY-RUN-REPORT.json`](dryrun-2026-10-13/DRY-RUN-REPORT.json).

## 1. What it is

- A Python CLI (`pf`, `src/pf/cli.py`) plus a runtime library (`src/pf_runtime/`) that turns **6 YAML specs** into a
  **Fivetran Connector SDK** connector that loads into the **Fivetran Managed Data Lake on S3** (Iceberg + Glue).
- Sources: `sftp`, `ftp` (FTPS), `rds` (PostgreSQL, MySQL). Destination: `s3` only.
- People own the specs and every decision. Everything else is deterministic code. The LLM is used in exactly
  **3 build-time steps**, never at runtime or in CI.
- Every command supports `pf --json <cmd>`, which returns one envelope `{api_version, command, ok, issues[], data}`.
  This is the contract the AI agents use (`src/pf/envelope.py`).

## 2. The six specs (people-owned, pydantic `extra=forbid`, `<REQUIRED>` placeholders never defaulted)

| File | Owner | Holds |
|---|---|---|
| `pipeline.yaml` | product owner | name, version (semver), GxP tier, 4 owners, requirement, environments, schedule, runtime |
| `source.yaml` | data engineer | connection (secrets only as `{secret: key}`), delivery `match` regex + `order_by`, readiness, redelivery, package transforms/checks |
| `destination.yaml` | data engineer (platform) | per env: Fivetran group, bucket, prefix, region, role ARN, Glue |
| `sttm.yaml` | data engineer + business | entities, attributes, types, transforms, attribute/row/entity checks (incl. English rules) |
| `quality.yaml` | quality owner | post-load lake checks (SELECT-only SQL), severity, ticket route |
| `policy.yaml` | governance | classification per column, access grants, retention, audit + originals buckets |

## 3. End-to-end flow, step by step (command → code → output → gate)

1. **`pf init-repo <dir>`** (`cli.cmd_init_repo`)
   - Writes `pf.toml` (repo marker), `pipelines/`, `.gitignore`, and `.claude/settings.json`. The settings deny
     agents `fivetran deploy`, `pf rules accept`, and edits to `build/**`, `releases/**`, `rules.lock.yaml`,
     workflows and CODEOWNERS.
2. **`pf new <p> --source ftp|sftp|rds --destination s3 [--entities ..] [--requirements doc]`** (`scaffold.py`)
   - Renders the 6 spec files from Jinja templates: defaults are filled, values only a person can supply are `<REQUIRED>`.
   - Security values (host keys, TLS, buckets, roles, classification, retention) are never defaulted.
   - With `--requirements`, it runs **intake** (`intake.py`), the **LLM step #1**:
     - Text is extracted deterministically (md/txt/docx/pdf, max 120k chars). Secrets are redacted and the
       document is fenced as data.
     - Skill `extracting-pipeline-requirements` returns a strict `RequirementProposal` JSON.
     - The deterministic merge fills **only placeholders or untouched scaffold defaults**, and adds an evidence
       comment to every value.
     - It never fills `policy.yaml`, `gxp_tier`, owners, buckets, roles, regions, host keys or TLS settings.
     - Single-column constraints become deterministic checks (regex, allowed_values, range) in `mode: shadow`.
       Cross-column rules stay `kind: english`.
     - It returns `open_questions` for owners, and saves `intake/<p>.proposal.json` and `intake/llm-calls.jsonl`.
3. **`pf validate <p> [--allow-unfilled]`** (`bundle.load_bundle` + `cross_validate`)
   - Runs pydantic validation per file, then cross-file checks: references, Fivetran naming, CEL syntax and
     names, secrets, TLS policy, GxP rules.
   - `--allow-unfilled` turns the result into the to-do list of `<REQUIRED>` values per file and owner.
4. **Owners fill and edit: `pf set | add | remove | add-check`** (`edit.py`)
   - Comment-preserving YAML edits. List items are selected by name or id, e.g.
     `entities[x].attributes[y].checks[z]`.
   - Each edit is re-validated and **rolled back** if it adds an error.
   - The quality owner flips the proposed checks from `shadow` to `enforce`.
   - Rule owners reword English rules to remove ambiguity and label **≥3 examples (a pass and a fail)**.
5. **`pf rules compile <p>`** (`rulecompiler.py`), **LLM steps #2 and #3**
   - Skill `compiling-english-rules` returns CEL, ambiguities and ≥6 boundary cases.
   - Deterministic verification follows:
     - the CEL parses and uses only known names;
     - every owner example and generated case is evaluated with the runtime's CEL engine;
     - mutation testing: flipped operators and dropped null allowances must be killed by the cases.
   - Skill `explaining-compiled-rules` (which never sees the English) back-translates the CEL.
   - The result is frozen in `rules.lock.yaml` with hashes of the text, examples, skill and prompt.
6. **`pf rules accept <p> <rule> --by @owner`**: a person only.
   - It is refused in agent sessions (`CLAUDECODE` / `PF_AGENT_SESSION`).
   - It re-verifies the rule and records who accepted it, their identity, a timestamp and an
     `acceptance_sha256` digest. Any later edit invalidates the acceptance.
7. **`pf build <p>`** (`generate/__init__.render_all`): a pure function, so byte-identical every time.
   - `lock.build_lock` resolves the specs plus the accepted rules into `spec.lock.json`, which the connector runs from.
   - `generate/connector.py` produces `connector.py` (thin, fixed: `schema()` and `update()` call
     `pf_runtime.engine`), the vendored `pf_runtime` (only the needed source adapters), `pf_rules`, a pinned
     `requirements.txt` and `configuration.example.json`.
     - It runs an **offline import check** (AST + pipreqs view): anything not pinned fails the build.
   - `generate/quality.py` produces `quality/checks.sql` and `checks.json` (post-load lake checks).
   - `generate/terraform.py` produces per-env Terraform:
     - pipeline level: Glue DB, Lake Formation grants, originals-writer policy, `columns/` tags;
     - repo level: `foundation/<env>/` with LF-tags, Object Lock buckets and CloudTrail.
   - `generate/docs.py` produces `contract.odcs.yaml` (ODCS v3.2.0), `README.md` and `fivetran/<env>.json`.
   - `generate/cicd.py` produces `.github/workflows/pf-<p>.yml`. `codeowners.py` writes the CODEOWNERS block
     (exactly one owner per governed file).
   - It also writes `BUILD-MANIFEST.json` (sha256 of every file). **`pf build --check`** regenerates in memory
     and fails on any difference.
8. **`pf test <p>`** (`testing.py`): break-and-verify with the **real runtime**.
   - `fixtures/good/` must load with no quarantine or errors, and a re-run with the saved state must emit
     nothing (idempotency).
   - Each `fixtures/bad/<case>/` must produce what its `expect.yaml` says: `status`, `rule_id`, `quarantined`.
   - Coverage: every enforced check must fire in at least one bad case. An uncovered check is an error for GxP
     tiers and a warning otherwise.
   - `RecorderOp` enforces the Fivetran tester contract. It writes `build/TEST-REPORT.json`.
   - Optional: `fivetran debug` (the official local tester), which writes DuckDB `files/warehouse.db`, then
     `pf dq run --engine duckdb`.
9. **`pf release <p>`** (`release.py`)
   - Writes `artifacts/<p>/<v>/connector.zip` (deterministic zip) and `release.json`, plus the committed record
     `releases/<v>.json`.
   - **`--check`** classifies the change against the previous record:
     - breaking (entity/attribute removed, type/key/load changed) → major;
     - additions and rule changes → minor;
     - documentation → patch.
   - A version can never be re-released with different content.
10. **Git: commit → pull request**. The generated workflow's `verify` job runs `pf validate`,
    `pf build --check`, `pf test` and `pf release --check`. CODEOWNERS routes each file to its owner for review.
11. **Merge → `deploy-<env>` jobs** (GitHub Environments, required reviewers on prod), **`pf deploy <p> --env <env>`** (`deploy.py`)
    - Installs the framework pinned in `pf.toml` with `-c build/connector/requirements.txt`, and assumes AWS by OIDC.
    - `pf release --upload s3://…` stores the artefact write-once (an identical re-upload is accepted).
    - `pf deploy` then:
      - refuses if the release needs a re-sync without `--acknowledge-resync`;
      - copies the build and proves it equals the release record;
      - runs the **dependency preflight** (installed versions must equal the pins);
      - assembles the full configuration from `PF_SECRET_<KEY>` into a 0600 temp file (masked in the logs);
      - runs `fivetran deploy … --naming fivetran --yes`;
      - fails if the SDK rewrote `requirements.txt`;
      - finally sends `PATCH /v1/connections/{id}` with the schedule and paused flag from `pipeline.yaml`.
    - One-time platform setup: `pf fivetran ensure-destination`, apply `foundation/<env>`, and apply the pipeline Terraform.
12. **Each Fivetran sync**: `update(configuration, state)` → `pf_runtime.engine.Sync.run`.
    - Validates the configuration. On empty state (first sync or re-sync), it rebuilds the loaded registry from
      the write-once `_loaded/` markers in the originals bucket.
    - **Files**: list → match regex → readiness (age/marker) → order by named groups → audit unmatched files.
      - Deliveries are prepared in parallel: download, size verify, sha256, archive the original write-once,
        structural checks, unzip safely, parse, transform, type, row/attribute checks (CEL), spool.
      - They are **emitted strictly in order**: data rows, `pf_keys_<entity>`, then the `pf_*` evidence tables.
        State is checkpointed after each delivery.
      - Redelivery, duplicate and late-data handling follow `on_redelivery`, `duplicate_package`,
        `late_data_policy` and `admit_late`.
    - **RDS**: read-only session, server-side cursor, incremental by a `>=` NOT NULL cursor column. A full
      reload commits only after the whole table has been read and checked.
    - On failure:
      - a quarantined row becomes `pf_quarantine` (row numbers only, never values);
      - a rejected delivery becomes `pf_package_audit.rule_id`;
      - a config error or exhausted retries end with a clean `op.error`.
    - Lineage columns on every row: `pf_package`, `pf_package_hash`, `pf_member`, `pf_row`, `pf_run_id`,
      `pf_pipeline_version`, `pf_key_hash`.
13. **Certify** (needs a webhook receiver, which is not included)
    - `sync_start` → `pf dq decertify`.
    - `sync_end` → `pf dq run --engine athena --certify --evidence-bucket …`, which:
      - runs the lake checks (fail closed);
      - verifies that Lake Formation governs access and that classified columns are tagged;
      - writes the evidence write-once;
      - sets the LF-tag `pf_certified=true` only if no blocker failed.
    - Consumers see certified tables only.
14. **Operate and change**
    - `pf status` shows the stage and the next action.
    - `pf fivetran drift` detects UI changes to the schedule or pause.
    - Every change goes back through validate → build → test → version bump → release → pull request.

## 4. Where the AI sits and how it is constrained

- **Headless steps** (`src/pf/llm/`, `steps-plugin/`): each step is a fresh Claude Agent SDK session.
  - Only the `Skill` tool, restricted to one skill, plus `StructuredOutput`. No MCP, subagents, settings or CLAUDE.md.
  - `permission_mode=dontAsk`, a PreToolUse deny hook, a pinned model (`PF_LLM_MODEL`), and turn, budget and time limits.
  - The answer is rejected if the skill was not actually loaded. Output is re-validated by pydantic.
- **Providers** (`PF_LLM_PROVIDER`):
  - `agent-sdk` calls the model.
  - `record` calls it and saves a fixture.
  - `replay` answers from `tests/llm-fixtures` (offline, deterministic, tamper-checked). The key covers the
    task, skill + skill hash, prompt, schema, prompt version and model.
- **Interactive** (`plugin/`): 4 skills drive `pf --json`, a read-only `pipeline-spec-reviewer` agent, and
  fail-closed hooks (`pf hook pre-tool|post-edit`) that block person-only commands and protected-file edits.

## 5. End-to-end run performed (2026-10-13)

Environment:
- Python 3.13 venv with `pip install -e '.[runtime,fivetran,agent,docs,dev]'` (fivetran-connector-sdk 2.12.1,
  claude-agent-sdk 0.2.165).
- Local stand-ins started by `dryrun/up.sh`: SFTP (asyncssh, pinned host key), FTPS (pyftpdlib + TLS),
  PostgreSQL 16, S3 (moto). A mock Fivetran REST API is used for deploy.
- LLM steps replayed from recordings (`--llm replay`).

Results:
- **Framework tests:** `pytest`: 270 passed, 14 skipped, 0 failed. The skipped tests need `PF_DRYRUN_ENV`.
- **End-to-end:** `python dryrun/run.py <state> --llm replay`: **91 / 91 checks passed** (sftp, ftp, rds,
  deploy, drills).

### Sample pipeline: `site_monitoring` (FTPS CSV feed), from requirement to deploy

| Step | What happened |
|---|---|
| `pf new … --source ftp --requirements site_monitoring.md` | intake drafted values with evidence and raised open questions. 3 deterministic checks were proposed in shadow (`site_no_format` regex, `visit_type_values`, `open_issues_non_negative`); 2 cross-column rules stayed in English |
| `pf validate --allow-unfilled` | 25 values left for owners (gxp_tier, owners, buckets, roles, retention, classification …) |
| `pf set …` | owners filled them. Checks were flipped to `enforce`. Both English rules were reworded and given 3 labelled examples each. An invalid edit (`source.tls yes`) was refused and rolled back |
| `pf rules compile` | `report_finalised_within_10_days` → `row.report_date == null \|\| (row.visit_date != null && row.visit_date <= row.report_date && …<= duration("240h"))`; `cov_zero_open_issues` → `row.visit_type == null \|\| row.visit_type != "COV" \|\| (row.open_issues != null && row.open_issues == 0)`. Both verified with no ambiguities |
| `pf rules accept` | refused for an agent session (`forbidden`); accepted by the owner `@rd-data-quality` |
| `pf build` / `pf test` | connector, checks, Terraform, contract, workflow and CODEOWNERS generated; 8 fixture cases passed |
| `fivetran debug` (official tester) over FTPS, verified TLS with a pinned CA | `SMV_20240301_01` (30 rows), `_02` (10), `20240302_01` (5) loaded in date/sequence order → **39 distinct visits** (the re-issue upserted 6). The 2-minute-old file was held by readiness. The tables `site_monitoring`, `pf_package_audit`, `pf_entity_audit`, `pf_rule_results`, `pf_quarantine` and `pf_keys_site_monitoring` were created |
| untrusted CA | connection refused (no silent MITM) |

### Release and deploy (run on `oncology_labs`)

- `release --check` failed until the release record was committed, then passed. The artefact upload was
  write-once and an identical re-upload was accepted.
- Deploy was refused for an agent session and when a secret was missing.
- `ensure-destination` created the Managed Data Lake destination. `--dry-run` masked the secrets.
- `fivetran deploy` uploaded the package (`POST /v1/connector-sdk/packages`) and created the connection with
  only the dev secrets, paused. The schedule PATCH (daily 02:00, paused) followed, and no drift was found.

### Other scenarios covered

- **SFTP `oncology_labs`:**
  - zip packages with 10 structural checks, 54 rows with pseudonymised ids;
  - originals archived under Object Lock (COMPLIANCE);
  - a bad checksum was rejected while the next delivery still loaded;
  - an idempotent re-sync;
  - late-data handling and `admit_late`;
  - a redelivery was rejected;
  - the DuckDB lake checks passed.
- **RDS `trial_sites`:**
  - intake from a .docx;
  - an incremental cursor: 250 rows, then only changed and new rows;
  - the reader role cannot write.
- **Drills:** wrong host key, bad private key, SFTP down, originals store down, missing salt, unknown
  environment, oversized delivery. Every one stopped cleanly with no data loaded.

## 6. Findings from the run

1. **The first dry run got 84/91. All 7 failures came from one deploy check (environment, not a code bug).**
   - pip installed `boto3/botocore 1.43.112`, but the connector pins `1.43.111`.
   - `pf deploy`'s dependency preflight correctly refused to deploy, as designed.
   - After installing with `-c build/connector/requirements.txt` (what the generated CI does), the rerun got 91/91.
   - The README's local install (`pip install -e '.[…]'`) does not apply these constraints. Add `-c` to the
     README/install docs, or pin boto3 in `pyproject.toml`.
2. **`tests/adversarial/test_cli.py:14` hardcodes `PF = "/home/claude/pipeline-factory/.venv/bin/pf"`**, so the
   test fails on any other checkout. Derive it from `sys.executable`, as `dryrun/run.py` does.
3. **`site_monitoring` in the dry run has 4 package checks uncovered**: `package_name`, `package_not_duplicate`,
   `package_not_empty` and `package_size` never fire in a bad fixture.
   - This is a warning only because the pipeline is `non_gxp`.
   - Add bad fixtures for them, as the shipped example does, before using a GxP tier.
4. **Known limitations (manual ch. 19), still open:**
   - no webhook receiver for certification;
   - RDS incremental reads cannot see hard deletes;
   - the Terraform has not been `plan`ned against a real account;
   - the dry run uses a mock Fivetran API and moto S3, so the first real dev deploy is the final verification.
