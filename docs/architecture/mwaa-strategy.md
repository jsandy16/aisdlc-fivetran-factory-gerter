# MWAA environment strategy

Decisions: [ADR-0006](../adr/0006-mwaa-control-plane.md) (central control plane, cross-account launch, publisher),
[ADR-0007](../adr/0007-airflow-version.md) (version), [ADR-0010](../adr/0010-event-triggers-and-sqs-custody.md)
(events).

## 1. Role of MWAA

MWAA **orchestrates only**. It does no data work and holds no team secrets:
- it launches EKS pods (through a team launcher role) and invokes Lambdas;
- it triggers Fivetran syncs;
- it waits with deferrable operators;
- it branches on run-state values.

The reasons:
- In a shared environment every DAG runs under the same execution role.
- Airflow 3 has no per-DAG restriction on which Connections a DAG may read.
- Task logs share one log group.
- `requirements.txt` is environment-wide, and changing it restarts the environment.

## 2. Where MWAA runs (decided: central per environment)

Each team has its own account per environment (dev, test, ppt, prod). MWAA runs **once per environment in a
platform account** and orchestrates every team's pipelines, cross-account.

| Option | Isolation | Cost (us-east-1, see §3) | Verdict |
|---|---|---|---|
| **A. Central MWAA per env in a platform account** | MWAA holds no data or secrets. Team isolation comes from cross-account launcher/trigger roles, per-team Airflow RBAC roles, pools and the central publisher. Data never leaves team accounts. | ≈ **$1,837/month** for all 4 environments, independent of the number of teams | **chosen** |
| B. MWAA in every team account per env | strongest; separate execution role, logs and requirements | ≈ $1,140/month **per team** (prod small, ppt small, test/dev micro); 10 teams ≈ $11.4k | rejected (cost, 4× upgrades per team) |
| C. Central MWAA + central EKS | cheapest compute | — | rejected: team data would be processed in the platform account |

**When a team gets a dedicated MWAA environment** (`platform-settings.mwaa.dedicated_teams`):
- a `gxp_direct` pipeline needing separately qualified orchestration;
- sustained concurrency above the shared environment's agreed share.

## 3. Sizing and cost

Prices are from the AWS Price List API (AmazonMWAA, us-east-1, published 2026-09-11), per hour.
Monthly = × 730 h. The environment price covers the base scheduler, web server and one worker. Additional workers,
schedulers and web servers are charged per hour. Confirm what each class includes on the pricing page.

| Class | Environment | + worker | + scheduler | + web server | Capacity (AWS docs) |
|---|---|---|---|---|---|
| mw1.micro | $0.29 | — (no scaling) | — | — | sandbox |
| mw1.small | $0.49 | $0.055 | $0.055 | $0.055 | small |
| mw1.medium | $0.74 | $0.11 | $0.11 | $0.055 | ≤ 250 DAGs, 10 tasks/worker |
| mw1.large | $0.99 | $0.22 | $0.22 | $0.11 | ≤ 1,000 DAGs, 20 tasks/worker |
| mw1.xlarge | $1.98 | $0.44 | $0.44 | $0.22 | ≤ 2,000 DAGs, 40 tasks/worker |
| mw1.2xlarge | $3.96 | $0.88 | $0.88 | $0.44 | ≤ 4,000 DAGs, 80 tasks/worker |

Metadata database storage: $0.10 / GB-month. MWAA Serverless: $0.08 per task-hour (1-minute minimum).

**Starting configuration (option A, 4 environments):**

| Env | Class | Workers min/max | Schedulers | Est. monthly |
|---|---|---|---|---|
| prod | mw1.medium | 2 / 10 | 2 | $540 (env) + ~$161 (avg 2 extra workers) + ~$2 (DB) ≈ **$703** |
| ppt | mw1.small | 1 / 3 | 2 | $358 + ~$20 ≈ **$378** |
| test | mw1.small | 1 / 3 | 2 | ≈ **$378** |
| dev | mw1.small | 1 / 3 | 2 | ≈ **$378** |
| **Total** | | | | **≈ $1,837/month** |

- **Growth.** Move prod to mw1.large at about 200 DAGs or sustained queueing (≈ $1,046/month with 2 extra workers).
- **Why worker counts stay low.** Workers are rarely the bottleneck: heavy work runs on Lambda or EKS in the team
  accounts and waits are deferrable.
- **Schedulers and triggerers.** Each scheduler also hosts a triggerer, so raise the scheduler count before the
  worker count when many deferrable tasks wait.
- **EKS cost is per team-env and optional.** A cluster (≈ $73/month control plane + Karpenter nodes, scaled to zero)
  is created in a team-env account only when one of the team's pipelines plans an `eks_pod` task. The worked example
  at 10 GB runs entirely on Lambda ([engine-selection.md §3](engine-selection.md)).

## 4. Execution role (least privilege)

The MWAA execution role (platform account) is granted:
- read the MWAA bucket (`dags/`, `plugins.zip`, `requirements.txt`);
- write its own CloudWatch log groups and metrics;
- `sts:AssumeRole` **only** on `arn:aws:iam::<team-account>:role/factory-launcher-<team>` for the team accounts of
  this environment (from `platform-settings`), always with the team's `ExternalId`;
- `secretsmanager:GetSecretValue` on `airflow/connections/*` and `airflow/variables/*` only (the native backend).

It is not granted any data bucket, staging, KMS key, Vault path or portal credential, in any account.

**Per-pipeline permissions** live in workload roles in the team accounts, assumed by Lambdas and pods
([ADR-0011](../adr/0011-workload-identity.md)).

## 5. Launching Lambdas and EKS pods in team accounts

- **Lambda tasks.** `LambdaInvokeFunctionOperator(aws_conn_id=<team launcher connection>)`. The connection holds
  the launcher role ARN + ExternalId (non-secret). The launcher role may invoke only `factory-*` functions in its own
  account.
- **EKS tasks.** `EksPodOperator(aws_conn_id=<team launcher connection>, cluster_name=<team cluster>)`. Through an
  **EKS access entry** in the team cluster, the launcher role maps to a Kubernetes group with RBAC to create pods
  **only in namespace `team-<team>`**. `aws-auth` is not used.
- **What may run.** A ValidatingAdmissionPolicy (or Kyverno) in each team namespace requires:
  - an image from the factory ECR, **by digest**, with a valid signature;
  - the command `python -m factory_runtime.run`;
  - a `serviceAccountName` whose `factory/pipeline` label matches the pod's label;
  - a `factory/bundle-sha` label that is in the namespace's allow-list (written by the team deploy role);
  - `readOnlyRootFilesystem`, non-root, no privilege escalation, no host mounts.
- **The central DAG publisher** (platform repo) regenerates each DAG from the team's committed spec with the trusted
  toolchain. It checks team, account, launcher role, pool and bundle hash, then writes `dags/<team>/<pipeline>.py`.
  Team CI never writes to the MWAA bucket.
- **Triggers from team accounts.** The team's dispatch Lambda assumes `factory-trigger-<team>` (platform account),
  which allows `airflow:InvokeRestApi` only as the team's Airflow RBAC role. That RBAC role can trigger only the
  team's DAGs, with `dag_run_id = run_key` ([ADR-0010](../adr/0010-event-triggers-and-sqs-custody.md)).
- **Logs.** Pod and Lambda logs stay in the team account's log groups (`get_logs=False` for pods); the MWAA task
  log carries a link and a summary.
- **Spikes (Phase 8, need a sandbox).**
  - S2: does a deferrable `EksPodOperator` refresh its EKS token cross-account for long pods? Otherwise use
    non-deferrable mode with an `eks_launch` pool.
  - S3: cross-account `InvokeRestApi` from a team Lambda, with duplicate `dag_run_id` rejection.

## 6. Connections, Variables and secrets

- Airflow Connections and Variables come from the **native `SecretsManagerBackend`**
  (`airflow/connections/*`, `airflow/variables/*`); nothing is stored in the metadata DB. They hold only platform
  items: Fivetran API key, notification hooks, team launcher role ARNs (non-secret).
- Pipeline secrets (Vault or Secrets Manager) are **never** resolved by Airflow. Pods and Lambdas resolve them
  through `SecretProvider` ([secret-provider.md](secret-provider.md)).
- **Spike S1** confirms which Airflow 3 component evaluates the secrets backend on MWAA (API server vs worker), and
  therefore which role needs the Secrets Manager permission.

## 7. Dependencies and upgrades

- **`requirements.txt`.** The platform repo owns it. It starts with
  `--constraint https://raw.githubusercontent.com/apache/airflow/constraints-3.3.1/constraints-3.12.txt` and adds
  only `factory_airflow` (whose dependencies are a subset of the constraints).
  - CI validates it in the MWAA local runner image of the same version: install, import every generated DAG, and
    check parse time.
- **Upgrades are blue/green.** Create the new environment, publish the same DAGs, move schedules team by team, then
  retire the old one. An in-place upgrade of a shared environment would affect every team at once.

## 8. DAG conventions (enforced by generator + DAG-integrity tests)

- **Parse time.**
  - No top-level I/O, API calls or heavy imports; enforced by an AST check of generated files.
  - Import budget measured; parse under 1 second.
- **Task settings.**
  - Deferrable operators and sensors wherever the provider supports them.
  - `retries`, `retry_exponential_backoff`, `execution_timeout` and `sla`/deadline alerts on every task.
  - `max_active_runs` and `max_active_tasks` per DAG.
- **Pools.**
  - `team_<team>` pool per team.
  - `portal_<name>` pools protect each portal.
  - An `eks_launch` pool if non-deferrable pod mode is used.
- **Identity and visibility.**
  - `dag_id = <pipeline_id>` (or `__v<major>` after a breaking change).
  - Tags: team, classification, gxp tier, spec version.
  - Access control: the DAG's team RBAC role.
  - `dag_run_id = run_key`; conf and XCom carry ids and hashes only.
- **Retirement.** Pause, drain in-flight runs, then delete through the publisher.
