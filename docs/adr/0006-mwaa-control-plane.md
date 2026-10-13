# ADR-0006: MWAA as a control plane only; shared environment per stage; central DAG publisher; namespace-scoped pod launch

- **Status:** Proposed (Phase 1 review)
- **Date:** 2026-10-13

## Context

In a shared MWAA environment:
- one execution role serves every DAG;
- Airflow 3 cannot restrict Connections per DAG;
- logs share a group;
- `requirements.txt` is global.

Teams need isolation, and MWAA costs a fixed amount per environment.

## Decision

- **No data work and no team secrets on MWAA.** MWAA does no data work and holds no team secrets: it launches pods (through per-team launcher roles), invokes Lambdas, triggers Fivetran and waits deferrably.
- **Environments:** one shared environment per stage by default (≈ $1.5k/month for dev, test and prod in us-east-1). A team gets a dedicated environment for gxp_direct isolation, sustained load, or team-specific providers.
- **Pod launch is scoped:**
  - EKS access entries map each team launcher role to its own namespace's RBAC;
  - an admission policy requires a signed factory image by digest, the factory entrypoint, a service account matching the pipeline label, and an allow-listed bundle hash.
- **Only the central DAG publisher (platform repo) writes `dags/<team>/`**, after re-verifying each DAG. Team CI never writes to the MWAA bucket.
- **Pod logs:** `get_logs=False`; pods log to their team log group.

## Consequences

- Small workers and a low MWAA cost; cross-team escalation is contained.
- The publisher is another component to build and run (Phase 5).
- Spike S2 must confirm token refresh for deferrable `EksPodOperator`.

## Alternatives considered

- *A dedicated environment per team:* about 5× the cost for 10 teams; kept as an option.
- *Running the data work on MWAA workers:* rejected, because of the shared role, secrets and capacity.
