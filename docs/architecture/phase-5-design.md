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

_Filled in at the end of Phase 5._
