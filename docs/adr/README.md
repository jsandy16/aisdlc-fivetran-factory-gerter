# Architecture decision records

Format: status, date, context, decision, consequences, alternatives.

**Review record:** the Phase 1 ADRs were reviewed by the user on 2026-10-13. The open questions were answered (decisions D1–D10 in [00-overview.md](../architecture/00-overview.md#5-assumptions-and-user-decisions)), and the amended set was approved with the Phase 2 plan. ADR-0012 is deferred by decision D3.

| ADR | Status | Decision |
|---|---|---|
| [0001](0001-framework-evolution.md) | Accepted | Evolve pipeline-factory 0.1.0 into a multi-engine framework; freeze pf |
| [0002](0002-repository-topology.md) | Accepted | Framework repo, one pipelines repo per team, platform repo |
| [0003](0003-spec-layout.md) | Accepted | Spec as six owner-scoped files merged into one model; logic separated from environment bindings |
| [0004](0004-execution-model.md) | Accepted | Tasks are ordered rule lists run by one engine; a framework image plus a per-pipeline bundle verified by hash; run pinning; typed staged artefacts |
| [0005](0005-engine-selection.md) | Accepted | Spec-driven engine selection by a deterministic planner |
| [0006](0006-mwaa-control-plane.md) | Accepted | Central MWAA per environment in a platform account (control plane only); cross-account launch into team accounts; central DAG publisher |
| [0007](0007-airflow-version.md) | Accepted | Airflow 3.3.1 on MWAA, pinned after a spike; 3.2 as fallback; blue/green upgrades |
| [0008](0008-secret-provider.md) | Accepted | Pluggable SecretProvider (Vault, Secrets Manager); resolved only in workloads; native SM backend for Airflow's own connections |
| [0009](0009-portal-integration.md) | Accepted | Portal as a first-class spec section; JMESPath; runtime-effective; returns destinations bounded by an allow-list |
| [0010](0010-event-triggers-and-sqs-custody.md) | Accepted | S3 event → SQS → dispatch Lambda (team account) → cross-account InvokeRestApi; custody snapshot; no AssetWatcher for GxP custody |
| [0011](0011-workload-identity.md) | Accepted | EKS Pod Identity by default (IRSA supported); per-pipeline workload roles in team accounts |
| [0012](0012-encryption.md) | Deferred | Package encryption: deferred (done outside the pipeline); design kept for later |
| [0013](0013-checksums-and-large-packages.md) | Accepted | Streaming packaging with checksums computed in flight and an independent re-check |
| [0014](0014-runtime-idempotency.md) | Accepted | Deterministic run keys, write-once targets, transactional sequence numbers, revisions for forced reruns |
| [0015](0015-account-model.md) | Accepted | Account per team per environment (dev, test, ppt, prod); platform account per environment |
| [0016](0016-network.md) | Accepted | Private-by-default networking; egress allow-lists generated from the destination and source allow-lists |
| [0017](0017-determinism.md) | Accepted | Byte-identical generation: canonical lock, toolchain image, no per-file stamps, .tf.json, deps lock separate |
| [0018](0018-change-engine.md) | Accepted | Incremental change from output diffs, explained by a semantic spec diff |
| [0019](0019-agents-and-orchestrator.md) | Accepted | LLM agents author; deterministic code decides; orchestrator is a state machine with gates |
| [0020](0020-custom-rule-plugins.md) | Accepted | Custom rules: typed contract, enforced boundaries, review and person acceptance |
| [0021](0021-gxp-controls.md) | Accepted | GxP / Part 11 / ALCOA+ controls built into the workflow |
| [0022](0022-llm-boundary.md) | Accepted | No real data reaches an LLM; configurable model provider |
| [0023](0023-observability.md) | Accepted | JSON logs with correlation ids, embedded metrics, error-class alert routing |
| [0024](0024-portal-resolved-destinations.md) | Accepted | Portal-resolved destinations constrained by a governance-owned allow-list |
