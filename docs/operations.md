# Operations

## One-time setup per environment (platform team)

1. Fivetran: a destination group per environment (`destination.environments.<env>.fivetran_group`). Create the
   Managed Data Lake destination with `pf fivetran ensure-destination <p> --env <env>` (or `--dry-run` to see the
   request). Bucket, prefix, region, role and Glue cannot change after creation.
2. AWS: apply `build/terraform/<env>/` (Lake Formation tags and grants, audit bucket and trail, originals bucket).
   Apply `terraform/<env>/columns/` after the first sync, when Glue has the columns.
3. GitHub: Environments named like the pipeline environments; required reviewers on prod. Variables
   `PF_AWS_ROLE_ARN`, `PF_AWS_REGION`, `PF_ARTEFACT_BUCKET`; secrets `FIVETRAN_API_KEY` (base64 `key:secret`) and
   one `PF_<KEY>` per configuration key in `build/connector/configuration.example.json` (environment-scoped, so
   dev and prod hold different values under the same name).
4. `pf.toml`: set `framework_requirement` to the framework release the repository uses.

## Normal change

Edit specs → `pf validate` → (`pf rules compile` + owner `pf rules accept` for English rules) → `pf build` →
fixtures → `pf test` → bump `version` → `pf release` → pull request. Merge deploys.

## After each sync

`pf dq run <p> --engine athena --output s3://... --certify --evidence-bucket <audit bucket>` runs the lake checks,
writes evidence, and sets `pf_certified=true` only if no blocker failed. `pf fivetran drift <p> --env <env>`
reports schedule or pause drift.

## When something goes wrong

| Symptom | Where to look | Action |
|---|---|---|
| Delivery `rejected` | `pf_package_audit.rule_id/reason` | vendor fixes it; a corrected checksum sidecar is picked up automatically; new content needs `on_redelivery` |
| Delivery `unmatched` | `pf_package_audit` (once per file) | misnamed/misplaced file: vendor renames, or widen `source.package.match` |
| Rows quarantined | `pf_quarantine` (row numbers, no values), `pf_rule_results` | fix at source, or change the rule (new version) |
| Sync failed: configuration | Fivetran sync log | missing/empty secret, wrong key, host key or certificate mismatch |
| Sync failed: source unavailable | Fivetran sync log | retried with backoff; the next sync resumes from the checkpoint |
| `deploy_tampered` in CI | job log | the SDK tried to change pinned requirements; review before re-running |
| `not_released` in CI | job log | build differs from the committed release record: rebuild or bump the version |

## Secrets

Never in specs or files: specs name configuration keys; CI injects `PF_SECRET_<KEY>`; `pf deploy` writes a 0600
temporary configuration, deletes it, and masks every value (and every line of PEM values) in its output.
Rotation: update the GitHub Environment secret and re-run the deploy job (a non-empty configuration replaces the
stored one, so every key is always sent).
