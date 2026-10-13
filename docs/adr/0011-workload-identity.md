# ADR-0011: EKS Pod Identity by default (IRSA supported); per-pipeline workload roles in team accounts

- **Status:** Accepted
- **Date:** 2026-10-13
- **Amended:** per-pipeline roles default (D7, D8).

## Context

Least privilege per pipeline is required. With an account per team per environment (D7), IAM role quotas (~1,000 per account) apply per team, not per organisation. The user had no preference between Pod Identity and IRSA (D8).

## Decision

- **Identity:** `identity.default: pod_identity` in platform settings; `irsa` is also generated.
- **Roles:**
  - one workload role per pipeline task in the team account, with exactly the actions derived from its ops on declared resources;
  - Lambda functions use their own per-function roles;
  - every role has the team permission boundary.
- **ABAC** on Pod Identity session tags is optional and no longer the default.
- **CI checks** with IAM Access Analyzer.

## Consequences

- The simplest least-privilege model.
- CloudTrail attributes actions to pipeline and pod.

## Alternatives considered

- *Team roles + ABAC:* kept as an option for teams with very many pipelines.
