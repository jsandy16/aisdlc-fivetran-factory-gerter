---
name: building-and-releasing-pipelines
description: Builds a pipeline-factory pipeline (Fivetran Connector SDK connector, post-load checks, Lake Formation and audit Terraform, CI workflow), proves it with break-and-verify fixture tests and the Fivetran local tester, and prepares the versioned release for review. Use when specs are valid and someone wants to build, test, debug, release or deploy a pipeline.
---

# Building and releasing pipelines

Deployment happens only in CI, after review, through GitHub Environments. You build, prove and prepare.

## Steps

1. `pf --json status <p>` — confirm the stage is `build`, `verify` or `deploy`; otherwise finish that stage first.
2. `pf --json build <p>` — deterministic; never edit anything under `build/` (a hook blocks it). If the build
   refuses because English rules are not accepted, stop and tell the user which owner must accept which rule.
3. Fixtures (synthetic only, never real data): `fixtures/good/` must load cleanly; each `fixtures/bad/<case>/`
   breaks one rule and has `expect.yaml` (`status: loaded|rejected|failed|unmatched`, `rule_id`, and/or
   `quarantined: {rule_id: rows}`). Database sources use `<entity>.csv` with source column names as header.
4. `pf --json test <p>` — runs the real runtime on the fixtures, checks idempotency, and lists checks that never
   fired (`uncovered`). Add a bad case for each uncovered check; for GxP tiers this is mandatory.
5. Optional local run with Fivetran's own tester (needs local copies of the source):
   `cd pipelines/<p>/build/connector && fivetran debug --configuration <local test config>`.
6. Version and record: bump `version` in pipeline.yaml if specs changed since the last release, then
   `pf --json release <p>` (writes `releases/<version>.json`). `pf --json release <p> --check` must pass.
7. Summarise for the pull request: what changed, check coverage, version bump reason, open owner decisions.
   Do not commit or push unless the user asks.

## Never

Run `fivetran deploy`, `pf deploy` without `--dry-run`, `pf rules accept`, or read `configuration.json` or
secrets. `pf deploy <p> --env <env> --dry-run` shows exactly what CI will do.

## Failures

Read `issues[].code`: `stale_build` → rebuild; `uncovered` → add bad fixtures; `expectation_failed` → the rule or
the fixture is wrong — ask the owner which; `rerun_failed` → a sync with nothing new failed (state or row-count
logic); `dependencies` → a rule module imports something unpinned; `version` → bump as instructed;
`not_released` (CI deploy) → the build differs from the committed release record.
