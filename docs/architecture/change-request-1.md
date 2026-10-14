# Change request 1: bring-your-own infrastructure, local secrets, local runs, services by configuration

Approved before Phase 8 B. Decision record: [ADR-0025](../adr/0025-infrastructure-ownership-and-resource-catalog.md);
the secrets amendment is in [ADR-0008](../adr/0008-secret-provider.md). Developer guide:
[local-development.md](../local-development.md).

| # | Milestone | Proven by |
|---|---|---|
| M1 | The environment chooses the secret store; local secrets file for local runs | provider tests (0600, missing key), per-environment resolution, refusal in prod (77), template merge, guard |
| M2 | `infrastructure`: create or existing per group, handover contract, release/adopt, `factory infra check` | generator matrix, offline `terraform validate` of `removed`/`import`, change plan release, IAM-simulator check |
| M3 | `factory run`: local stores, real or local data, local secrets, per-task report | the worked example delivered locally with a separate-process stand-in for the dev account |
| M4 | Resource catalog (9 types), alert routing by configuration, `notify.publish` | every type renders, validates and passes checkov; a new type as data only; a spec-only topic + notification |

## As built (differences from the approved plan, and why)

- **M1, no schema migration.** The store declared with a secret became an optional fallback instead of being moved,
  so existing specs keep working unchanged. `local_file` is not a value a deployed environment can choose: a deployed
  task can never read a developer's laptop. Local runs read every secret from the file, and deployed environments
  choose Vault or Secrets Manager.
- **M3.** Pod tasks run in-process, like Lambda tasks; running the runtime image in Docker is not added. There is no
  developer profile file: the command-line options cover it. Local stores use a local moto endpoint. The one call
  that crosses stores and data (the server-side copy from staging to a real destination) becomes a ranged read plus
  an upload part, only in local runs.
- **M4.** Instances are declared in `environments.yaml` → `resources` rather than a seventh spec file (bindings, so
  `logic_sha` is unchanged; smaller change to the loader, schemas and locks). SLA alerts stay with Airflow; the
  dead-letter alarm follows the configuration channel. The runtime now emits `PipelineFactory/TaskFailed` per
  failure class for the alarms.
- **Locks:** the binding models gained fields, so every pipeline's `binding_sha` changed; `logic_sha` did not.
- **Tests:** 699 passed on 3.13 and 3.11; opt-in Airflow + IaC: 9 passed (ownership stacks, every catalog type with checkov).
