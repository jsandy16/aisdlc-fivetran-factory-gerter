# ADR-0007: Airflow 3.3.1 on MWAA, pinned after a spike; 3.2 as fallback; blue/green upgrades

- **Status:** Accepted
- **Date:** 2026-10-13

## Context

MWAA supports Airflow 3.3.1 (GA Sep 2026), 3.2 and 2.11.2. Airflow 3 brings the Task SDK, DAG versioning, assets and deferrable improvements. 3.3.1 has been on MWAA for about one month.

## Decision

- **Target version.** Generate for Airflow 3.3.1 using the official `constraints-3.3.1/constraints-3.12.txt`. Provider pins from it: amazon 9.34.0, cncf-kubernetes 10.21.0, common-messaging 2.0.4, hashicorp 4.8.0.
- **Pin after the spike.** The version is pinned only after spike S1 passes. Fallback is 3.2.
- **`factory_airflow`** depends only on packages in the constraints.
- **Upgrades are blue/green.** A new environment is created, DAGs are republished, schedules move team by team, and the old environment is retired.

## Consequences

- The newest features are available; a little maturity risk is accepted.
- The upgrade path is controlled and never in place for a shared environment.

## Alternatives considered

- *2.11.2:* rejected, because it is the legacy line without Airflow 3 features.
