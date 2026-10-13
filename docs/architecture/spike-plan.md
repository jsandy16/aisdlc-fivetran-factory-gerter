# Spike plan

Spikes are throwaway experiments that prove or disprove an assumption. **They start only after the Phase 1 review**
(the brief says no code before the architecture is agreed). Each spike happens at the start of the phase that needs
its answer.

## Local spikes (no AWS account needed)

| Id | Question | Method | Acceptance criteria | Phase |
|---|---|---|---|---|
| S1 | Does a generated DAG work on Airflow 3.3.1 with MWAA's constraints? Which Python version? Which component evaluates the secrets backend? | MWAA local runner image for 3.3.1 (or `apache/airflow:3.3.1` + constraints); install `factory_airflow`; import a sample DAG; `dag.test()` with mocks | install clean against the constraints; DAG parse < 1 s; no top-level I/O; secrets-backend behaviour documented | 2 (start) |
| S4 | Can the admission policy stop a pod with the wrong SA, image or label? | kind cluster + ValidatingAdmissionPolicy (and Kyverno variant); per-namespace RBAC mimicking access entries | all negative cases rejected; allowed case admitted | 5 |
| S6 | Throughput and memory of streaming S3 download → zip → multipart, in Lambda-sized memory | moto S3 server; 10 GB synthetic tree (D10) | memory below the ceiling (part size × concurrency + 256 MB); throughput recorded for the planner's resource model; deterministic zip bytes across runs | 3 (start) |
| S7 | Vault KV v2 through hvac with caching and rotation | Vault dev server | read by version; rotation mid-run handled; audit events have no value material | 3 |
| S8 | Byte-identical generation across OSes | toolchain image on Linux + macOS runner | identical `BUILD-MANIFEST` | 5 |

## AWS spikes: Phase 8 (need the sandbox account, decision D6)

| Id | Question | Acceptance criteria |
|---|---|---|
| S2 | Does a deferrable `EksPodOperator` refresh its EKS token **cross-account** (platform MWAA → team cluster) for pods running longer than 15 minutes? | a 2-hour pod completes in deferrable mode, or the fallback is adopted |
| S3 | Team-account Lambda ESM → `factory-trigger-<team>` (platform account) → `InvokeRestApi` with `dag_run_id = run_key`: is a duplicate rejected (409)? Is the team RBAC role limited to its own DAGs? Latency? | duplicates rejected; p95 trigger latency recorded |
| S5 | Pod Identity per-pipeline roles in a team account; session tags in CloudTrail | cross-pipeline access denied; CloudTrail shows namespace, SA and pod |
| S9 | Native Secrets Manager backend on MWAA 3.3.1 for Connections; `airflow/*` scoping | connection resolved; no other secrets readable by the execution role |
| S10 | Fivetran REST provisioning of a native connector from CI (credentials never in Terraform state) | the connection is created, and no credential appears in state |

S6 is repeated in Phase 8 on real Lambda and EKS to calibrate the planner's resource model (the constants in
`engine_table.v1.json` / `op_catalog.v1.json` are placeholders until then).
