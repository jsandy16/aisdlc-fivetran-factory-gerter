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

## As built

- **M0 not done:** still no `ANTHROPIC_API_KEY` in this environment.
- **Change engine** (`factory/change/`): the base is a git ref (`git ls-tree` + `git show`, no checkout) or a folder,
  copied into a scratch repo that shares this repo's platform settings and price basis. Both sides are rendered by
  the same code as `factory build` (`gen/build.render`), so the file and Terraform deltas are exact. The plan also
  refuses `pipeline_id` changes and a changed spec without a higher `spec_version` (`spec_version_not_bumped`).
  `--record` writes `changes/<n>.json` only when the plan has no issues; the BA guard hook refuses edits to it.
- **DAG files are named by `dag_id`** (`dags/<env>/<pipeline>__v2.py` after a major bump), so the old and new DAG
  can coexist while the old one drains; the dispatcher's `FACTORY_DAG_ID` follows.
- **verify** tells `generated_file_edited` (file differs from its own manifest hash) from `build_stale` (consistent
  build, changed spec). `BUILD-MANIFEST.json` now records `dag_id`, `spec_version`, `image` and the price basis + sha.
- **Prices:** `Repo.prices` is the calibrated file or the catalog, never a mix; a calibrated file must say
  `status: calibrated`. `factory plan`, `cost`, `build` and `change plan` all use it; reports carry `basis` and `note`.
- **Docs:** `docs/runbook.md`, `lineage.md` (Mermaid), `alerts.md` in every build; ids, names and secret names only.
- **Pods** now map exit codes in `cleanup` (75 or no exit code → retried; others fail at once); proven in the real
  Airflow DagBag probe. **CI:** `factory build` + `verify` in the main job; new `factory-opt-in` job runs
  `-m "airflow or iac"`. **tflint** still not added: GitHub is blocked here, so no version or checksum could be pinned.
- **Tests:** 663 passed on 3.13 and 3.11; opt-in Airflow + IaC: 5 passed.
