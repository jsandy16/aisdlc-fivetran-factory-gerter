# MWAA environment strategy

Decisions: [ADR-0006](../adr/0006-mwaa-control-plane.md) (control plane, sharing, publisher),
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

## 2. Shared vs per-team environments

| Option | Isolation | Ops burden | Cost (us-east-1, see §3) | Verdict |
|---|---|---|---|---|
| **A. One shared environment per stage** | pools, DAG tags, RBAC roles, per-team launcher roles, admission policy, central publisher | lowest: 3 environments | ≈ $1.5k/month for dev + test + prod | **default** |
| B. One environment per team per stage | strongest: separate execution role, logs and requirements | high: 3 × teams environments, upgrades × teams | ≈ $0.8k/month **per team** (10 teams ≈ $7.8k) | only where needed |
| C. Hybrid (A + dedicated for selected teams) | as A, plus B for teams that need it | medium | A + ~$0.8k per dedicated team | **recommended policy** |

**When a team gets a dedicated environment** (`platform-settings.mwaa.dedicated_teams`):
- `gxp_direct` pipelines needing an isolated, separately qualified environment;
- sustained concurrency above the shared environment's agreed share;
- team-specific providers or requirements.

In the `account_per_team` model, A means one shared environment per stage *per team account*, which is in effect B.

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

**Starting configuration (option A):**

| Stage | Class | Workers min/max | Schedulers | Est. monthly |
|---|---|---|---|---|
| prod | mw1.medium | 2 / 10 | 2 | $540 (env) + ~$161 (avg 2 extra workers) + ~$2 (DB) ≈ **$703** |
| test | mw1.small | 1 / 3 | 2 | $358 + ~$20 ≈ **$378** |
| dev | mw1.small | 1 / 3 | 2 | ≈ **$378** |
| **Total** | | | | **≈ $1,459/month** |

- **Growth.** Move prod to mw1.large at about 200 DAGs or sustained queueing (≈ $1,046/month with 2 extra workers).
- **Why worker counts stay low.** Workers are rarely the bottleneck because heavy work runs on EKS or Lambda and
  waits are deferrable.
- **Schedulers and triggerers.** Each scheduler also hosts a triggerer, so the scheduler count is raised before the
  worker count when many deferrable tasks wait.
- **Option B for comparison** (per team): prod mw1.small $358 + dev/test mw1.micro 2 × $212 ≈ **$782/month per
  team**.

## 4. Execution role (least privilege)

Granted:
- read the MWAA bucket (`dags/`, `plugins.zip`, `requirements.txt`);
- write its own CloudWatch log groups and metrics;
- `sts:AssumeRole` **only** on `arn:aws:iam::<acct>:role/factory-launcher-<team>`, under the team permission
  boundary (resource pattern from `platform-settings`);
- `secretsmanager:GetSecretValue` on `airflow/connections/*` and `airflow/variables/*` only (the native backend).

Not granted: S3 data buckets, staging, KMS team keys, Vault, portal credentials.

**Per-pipeline permissions** are not added to this role. They live in workload roles assumed by pods and Lambdas
([ADR-0011](../adr/0011-workload-identity.md)).

## 5. Launching EKS pods safely from a shared environment

- **Who may launch where.** `EksPodOperator(aws_conn_id=<team launcher connection>)` assumes
  `factory-launcher-<team>`. Through **EKS access entries** that role maps to a Kubernetes group with RBAC to create
  pods **only in namespace `team-<team>`**. `aws-auth` is not used.
- **What may run.** A ValidatingAdmissionPolicy (or Kyverno) in each team namespace requires:
  - an image from the factory ECR, **by digest**, with a valid signature;
  - the command `python -m factory_runtime.run`;
  - a `serviceAccountName` whose `factory/pipeline` label matches the pod's label;
  - a `factory/bundle-sha` label that is in the namespace's allow-list (written by the deploy plane);
  - `readOnlyRootFilesystem`, non-root, no privilege escalation, no host mounts.
- **The central DAG publisher** (platform repo) regenerates each DAG from the team's committed spec with the trusted
  toolchain. It checks team, namespace, service account, pool and bundle hash, and only then writes
  `dags/<team>/<pipeline>.py`. Team CI never writes to the MWAA bucket.
- **Pod log shipping.** `get_logs=False`: pods write to their team log group, and the task log carries a link and a
  summary.
- **Spike S2.** Confirm that a deferrable `EksPodOperator` refreshes its EKS token for multi-hour pods. Otherwise use
  non-deferrable mode with a sized `eks_launch` pool.

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
