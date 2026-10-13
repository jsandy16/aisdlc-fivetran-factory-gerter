# Risk register

Likelihood (L) and impact (I) are rated H/M/L. Owner is the role that tracks the risk.

| # | Risk | L | I | Mitigation | Verified by | Owner |
|---|---|---|---|---|---|---|
| R1 | Packages of hundreds of GB exceed memory or disk, or run for hours | M | H | streaming zip → ESDK → multipart with bounded memory; planner sizing and `activeDeadlineSeconds`; `store` for compressed inputs; fused-task suggestion | spike S6 (50 GB benchmark), Phase 3 memory-ceiling test | framework |
| R2 | Checksum semantics are misunderstood (ETag ≠ MD5 for KMS/multipart objects) | H | M | checksums computed while streaming + independent re-read; sidecar objects + manifest; never ETag | Phase 3 op tests | framework |
| R3 | SQS messages are lost or duplicated (AssetWatcher deletes on receipt; standard queues deliver at least once) | H | H | Lambda ESM dispatch or in-task custody transfer; dedupe in run-state; `dag_run_id = run_key`; DLQ | Phase 3 race tests, spike S3 | framework |
| R4 | Cross-team escalation through the shared MWAA execution role | M | H | per-team launcher roles, namespace-scoped access entries, admission policy, central DAG publisher | spike S4 (kind + policy), Phase 5 tests | platform |
| R5 | Deferrable `EksPodOperator` token expires on long pods | M | M | spike; fallback to non-deferrable mode with an `eks_launch` pool | spike S2 | platform |
| R6 | A deploy during a run mixes versions | M | H | run pinning (bundle, image, logic sha); new `dag_id` for breaking changes; retirement protocol | Phase 5 tests | framework |
| R7 | Secret leakage (templated fields, conf, XCom, logs, TF state, exception text) | M | H | `SecretValue`, redaction filter, references-only conf/XCom, no pod env secrets, Fivetran creds via REST, canary scan in CI | Phase 3 canary test | security |
| R8 | Vault outage or rotation breaks runs | M | M | in-process cache with TTL; rotation overlap > TTL + task duration; refetch on auth failure; Airflow independent of Vault | Phase 3 rotation test | platform |
| R9 | AWS quotas (IAM roles ~1,000, policy size, Pod Identity associations, Lambda concurrency, KMS rates) | H | M | team roles + ABAC; per-pipeline roles only for gxp_direct; reserved concurrency; S3 Bucket Keys | Phase 5 IAM generator tests; capacity plan | platform |
| R10 | LLM-authored custom code is wrong or malicious; prompt injection from requirement docs | M | H | fenced input, typed outputs, AST allow-list, declared egress, default-deny network, review agent + mutmut + person acceptance | Phase 4 injection corpus | framework |
| R11 | Airflow 3.3.1 is new on MWAA (≈ 1 month GA) | M | M | spike in the MWAA local runner; 3.2 fallback; blue/green upgrades | spike S1 | platform |
| R12 | GxP validation effort grows with every pipeline | H | M | validate the framework once; generated per-pipeline evidence; zero-diff upgrades; golden corpus | Phase 6/7 | QA |
| R13 | Portal returns unexpected paths or hosts (SSRF, exfiltration) | L | H | `allowed_roots`, host from the spec only, schema fail-fast, `materialized` mode for gxp_direct | Phase 3 fault tests | security |
| R14 | Determinism broken by toolchain drift (pydantic schema output, Terraform providers) | M | M | toolchain image by digest; toolchain lock in the build hash; cross-OS verify | Phase 2/5 | framework |
| R15 | The undecided account model forces rework later | M | M | placement as a platform setting; generators support both models from Phase 5 | Phase 5 | platform |
| R16 | No AWS sandbox: AWS-only behaviour unproven until Phase 7 | H | M | local spikes now; AWS spikes listed with acceptance criteria; Q5 | spike plan | programme |
| R17 | BA adoption: the spec is too technical | M | M | `--view ba`, section-by-section intake, starters, plain-English errors, BA walkthrough in Phase 2 | Phase 2 exit | product |
| R18 | pf 0.1.0 and v2 diverge (two spec dialects) | M | L | pf frozen; factory → pf-lock adapter; `factory import-pf` later | Phase 5 | framework |
