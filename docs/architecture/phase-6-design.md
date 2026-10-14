# Phase 6 design: change engine, verify, cost basis, generated docs (approved plan of record, offline)

No AWS in this phase (D6). Decision records: [ADR-0017](../adr/0017-determinism.md),
[ADR-0018](../adr/0018-change-engine.md); detail in [determinism-and-change.md](determinism-and-change.md).

| # | Milestone | Proven by |
|---|---|---|
| M0 | Live agent recordings if `ANTHROPIC_API_KEY` is in the environment (otherwise skipped and reported) | recordings with real provenance |
| M1 | Change engine: `factory change plan <pipeline> --base <git ref or folder> [--record "request"]`. Semantic diff by stable ids (*why*), output diff from two in-memory builds (*what files*), Terraform resource diff per environment (*what infrastructure*), custom rules to re-review, tests to run, DAG strategy, approvals, cost delta. `--record` writes `changes/<n>.json` | breaking change without a major bump refused; `__v<major>` DAG; worked example change plans |
| M2 | `factory verify <pipeline>`: regenerate in memory and compare with `build/<pipeline>/` | hand edit, stale build, missing and unexpected files each reported; clean build passes |
| M3 | Cost basis: placeholder (catalog) and calibrated (platform `pricing.json`) prices never mixed; every figure says which; cost delta in each change plan | calibrated file changes the figures and the label |
| M4 | Generated docs per pipeline: `docs/runbook.md`, `docs/lineage.md`, `docs/alerts.md` | deterministic; content from the spec only |
| M5 | Carry-overs: pod exit-code mapping (75 retried, other codes fail at once); CI job for the opt-in Airflow and IaC checks; tflint if reachable | `-m airflow` probe; workflow file |

Decisions (defaults agreed):

- **Breaking** = a change to DAG shape or run identity: a task added or removed, `depends_on` changed, the trigger
  type changed, or `reliability.run_key` changed. It needs a major `spec_version` bump; the new DAG is
  `<pipeline>__v<major>` (major ≥ 2; major 0 and 1 keep `<pipeline>`), and the old DAG is paused, drained and
  retired. Everything else keeps the `dag_id`; run pinning protects in-flight runs.
- **Approvals** flagged, never granted here: security or destination changes → governance; breaking changes → platform;
  destroy of a stateful resource (queues) → platform. Approvers are recorded from the PR, not by the CLI.
- **Infrastructure delta offline** is a resource-address diff of the generated `.tf.json` (create / update / destroy);
  `terraform plan -json` against real state is Phase 8.
- **Calibrated prices** come from the platform team (`<platform settings>/pricing.json`, `status: calibrated`);
  without it the catalog placeholders are used. A change plan prices both sides with the same basis.
