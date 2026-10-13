# ADR-0011: EKS Pod Identity (recommended) or IRSA; team roles with ABAC; per-pipeline roles for gxp_direct

- **Status:** Proposed (Phase 1 review)
- **Date:** 2026-10-13

## Context

Least privilege per pipeline is required. IAM has an account quota of about 1,000 roles. The brief names IRSA.

## Decision

- **Both identity options are generated** (`identity: pod_identity | irsa` in platform settings). **Pod Identity is recommended:**
  - no per-cluster OIDC trust;
  - session tags (namespace, service account, pod name);
  - deterministic pod names that link CloudTrail to runs.
- **Roles:**
  - default: one workload role per team, with ABAC on the `factory/pipeline` principal tag scoping S3 prefixes, DynamoDB leading keys and secret paths;
  - `gxp_direct`: dedicated per-pipeline roles;
  - every role has a team permission boundary.
- **CI checks** with IAM Access Analyzer.

## Consequences

- Pipeline granularity is kept within quotas.
- The default is decided at the Phase 1 review (Q7).

## Alternatives considered

- *A role per pipeline per engine:* rejected, because it exceeds quotas at scale.
