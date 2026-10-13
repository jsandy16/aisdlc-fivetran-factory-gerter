# ADR-0006: Central MWAA per environment in a platform account (control plane only); cross-account launch into team accounts; central DAG publisher

- **Status:** Accepted
- **Date:** 2026-10-13
- **Amended:** central MWAA in a platform account; EKS/Lambda in team accounts (D7).

## Context

Each team has its own AWS account per environment (D7). In MWAA, one execution role serves every DAG; Airflow 3 cannot restrict Connections per DAG; requirements are environment-wide. MWAA costs a fixed amount per environment.

## Decision

- **One MWAA environment per environment** (dev, test, ppt, prod), in a platform account. It does no data work and holds no team secrets: it invokes Lambdas and launches pods in team accounts, triggers Fivetran syncs, and waits deferrably.
- **Cross-account launch.** The execution role may only assume `factory-launcher-<team>` in team accounts (with an ExternalId). That role invokes the team's `factory-*` Lambdas and, through an EKS access entry, creates pods only in the team namespace.
- **Admission policy** in team clusters: a signed factory image by digest, the factory entrypoint, a matching service-account label, and an allow-listed bundle hash.
- **Only the central DAG publisher** writes `dags/<team>/` in the MWAA bucket.
- **Cost:** ≈ $1,837/month for 4 environments (us-east-1). EKS clusters exist per team-env only when needed.

## Consequences

- Team data never enters the platform account.
- A small, cheap control plane.
- Cross-account roles to manage; spikes S2 and S3 (Phase 8) confirm token refresh and cross-account `InvokeRestApi`.

## Alternatives considered

- *MWAA per team account:* ≈ $1.1k/month per team and 4× the upgrades.
- *Central EKS too:* team data would be processed in the platform account.
