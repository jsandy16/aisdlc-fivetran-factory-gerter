# Phase 5 design: generation, infrastructure and build (approved plan of record, offline)

No AWS in this phase (D6): everything is generated and checked offline; deployment is Phase 8.

| # | Milestone | Proven by |
|---|---|---|
| M0 | Live agent recordings once `ANTHROPIC_API_KEY` is in the environment; opt-in `-m live` re-record | recordings with real provenance |
| M1 | `factory_airflow`: DAG generator (one DAG per pipeline, `dag_id = <pipeline_id>`; Lambda invoke mapping exit codes; Kubernetes pod with `skip_on_exit_code`; deferrable Fivetran wrapper; retries, pools, tags from the spec; conf with ids and hashes only) | DAG integrity tests (parse < 1 s, no top-level I/O, graph = spec) |
| M2 | Terraform generator (`.tf.json`): IAM from op contracts scoped to bound resources; Lambda sized by the planner; EKS service account, quota and egress allow-list; queue + DLQ + dispatcher mapping; staging, run-state and audit stores | `terraform validate`, tflint, checkov offline where installable; no wildcard actions |
| M3 | `factory cost`: planner unit costs × runs/month + MWAA share vs the spec's budget | worked example within budget or flagged |
| M4 | `factory bundle`, `factory test` (incl. custom rules), `factory build` (validate → plan → DAG → Terraform → cost → docs → review package, deterministic `BUILD-MANIFEST`) | byte-identical rebuild |
| M5 | Integration test: the generated DAG's task commands run in order against the stand-ins | worked example delivers end to end |
| M6 | Interactive Claude Code plugin for BAs: skills driving `factory --json`, read-only spec reviewer, hooks denying `factory accept`, generated-file edits and secret reads | hook tests |

Defaults agreed: Airflow 3.3.1 in an opt-in test group (`-m airflow`); terraform/tflint/checkov installed if the
network allows, otherwise structural tests + checkov with the gap reported; one end-to-end path, short notes.

## As built

- **M0 not done:** no `ANTHROPIC_API_KEY` in this environment; recordings stay synthetic until it is added.
- **Layout:** generators live in the build-time CLI package (`factory/gen/`: names, dag, terraform, cost, build);
  `packages/factory_airflow` holds only the operators the DAGs import on MWAA (plus `argv`, importable without
  Airflow). The BA plugin is `plugins/factory-ba/` (skill, read-only reviewer agent, fail-closed guard hook).
- **DAGs:** Lambda tasks map the runtime exit code (75 retried; 65/70/77/78 fail at once). Pods retry on any
  failure (KubernetesPodOperator cannot tell exit codes apart without more work; a Phase 8 item). Fivetran/MWAA
  tasks are a reschedule-mode sensor; each poke invokes the runtime Lambda with `FACTORY_POLL_BUDGET_S`, so no
  secret ever reaches Airflow. Loaded in a real Airflow 3.3.1 DagBag (`-m airflow`).
- **Terraform:** two files per pipeline and environment (`providers.tf.json`, `main.tf.json`) because checkov's JSON
  parser rejects provider blocks. KMS is scoped with `kms:ResourceAliases` (IAM cannot name keys by alias). VPC
  ENI actions need `Resource: "*"` (IAM limitation). Team stores and KMS keys are platform resources, referenced,
  not created. `terraform validate` runs offline against a local AWS provider 6.66.0 mirror
  (`scripts/install-iac-tools.sh`); tflint is not available (GitHub blocked here). Two reviewed checkov waivers in
  `platform/iac-waivers.yaml`.
- **Cost:** adds `mwaa_share_per_pipeline_month` (placeholder) to the pricing file. Worked example: about
  $25.81/month (budget $300).
- **Integration:** the worked example runs end to end from the generated DAG's own task commands.
- **Open for Phase 6+:** live recordings; pod exit-code mapping; tflint in CI; real prices.
