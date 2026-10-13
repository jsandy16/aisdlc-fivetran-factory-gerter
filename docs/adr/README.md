# Architecture decision records

Format: status, date, context, decision, consequences, alternatives. All are **Proposed** until the Phase 1 review.

| ADR | Decision |
|---|---|
| [0001](0001-framework-evolution.md) | Evolve pipeline-factory 0.1.0 into a multi-engine framework; freeze pf |
| [0002](0002-repository-topology.md) | Framework repo, one pipelines repo per team, platform repo |
| [0003](0003-spec-layout.md) | Spec as owner-scoped files merged into one model; logic separated from environment bindings |
| [0004](0004-execution-model.md) | Tasks are ordered rule lists run by one engine; a framework image plus a per-pipeline bundle verified by hash; run pinning; typed staged artefacts |
| [0005](0005-engine-selection.md) | Spec-driven engine selection by a deterministic planner |
| [0006](0006-mwaa-control-plane.md) | MWAA as a control plane only; shared environment per stage; central DAG publisher; namespace-scoped pod launch |
| [0007](0007-airflow-version.md) | Airflow 3.3.1 on MWAA, pinned after a spike; 3.2 as fallback; blue/green upgrades |
| [0008](0008-secret-provider.md) | Pluggable SecretProvider (Vault, Secrets Manager); resolved only in workloads; native SM backend for Airflow's own connections |
| [0009](0009-portal-integration.md) | Portal as a first-class spec section; JMESPath; untrusted paths; dispatcher Lambda; dynamic or materialized mode |
| [0010](0010-event-triggers-and-sqs-custody.md) | Events via Lambda dispatch or in-task custody transfer; no AssetWatcher for GxP custody |
| [0011](0011-workload-identity.md) | EKS Pod Identity (recommended) or IRSA; team roles with ABAC; per-pipeline roles for gxp_direct |
| [0012](0012-encryption.md) | AWS Encryption SDK with KMS by default; Vault Transit or Vault KV keys and OpenPGP as explicit options |
| [0013](0013-checksums-and-large-packages.md) | Streaming packaging with checksums computed in flight and an independent re-check |
| [0014](0014-runtime-idempotency.md) | Deterministic run keys, write-once targets, transactional sequence numbers, revisions for forced reruns |
| [0015](0015-account-model.md) | AWS account model as a platform setting supporting shared-per-env and account-per-team |
| [0016](0016-network.md) | Private-by-default networking with egress allow-lists generated from declared op egress |
| [0017](0017-determinism.md) | Byte-identical generation: canonical lock, toolchain image, no per-file stamps, .tf.json, deps lock separate |
| [0018](0018-change-engine.md) | Incremental change from output diffs, explained by a semantic spec diff |
| [0019](0019-agents-and-orchestrator.md) | LLM agents author; deterministic code decides; orchestrator is a state machine with gates |
| [0020](0020-custom-rule-plugins.md) | Custom rules: typed contract, enforced boundaries, review and person acceptance |
| [0021](0021-gxp-controls.md) | GxP / Part 11 / ALCOA+ controls built into the workflow |
| [0022](0022-llm-boundary.md) | No real data reaches an LLM; configurable model provider |
| [0023](0023-observability.md) | JSON logs with correlation ids, embedded metrics, error-class alert routing |
