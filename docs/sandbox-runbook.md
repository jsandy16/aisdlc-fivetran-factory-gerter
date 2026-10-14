# Sandbox runbook (Phase 8): deploy, run the spikes, tear down the same day

**Only in a dedicated sandbox account. Synthetic data only.** Everything is refused unless the caller's account equals
`FACTORY_SANDBOX_ACCOUNT_ID`, and the Terraform provider repeats that check (`allowed_account_ids`).

## 0. Before you start

- **Credentials:** add them in the environment settings (Network secrets / environment variables), never in chat:
  `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (and `AWS_SESSION_TOKEN` if temporary), plus
  `FACTORY_SANDBOX_ACCOUNT_ID`. The network policy must allow the AWS endpoints of the region.
- **Tools:** Docker (for the image), Terraform 1.16 (`scripts/install-iac-tools.sh`), `kubectl`, uv.
- **Cost:** about **$0.82 per hour** for the always-on parts (MWAA mw1.small ≈ $0.49, EKS control plane $0.10,
  two t3.large ≈ $0.18, NAT ≈ $0.05; approximate list prices), plus usage. A full day is about $20. The budget alarm
  (`budget_usd`, default $50 a month) emails at 50% and 80%.
- **Check:** `uv run python -m factory_testing.sandbox preflight` must print the account (last four digits) and
  "reachable" for MWAA, EKS and ECR. Also confirm that MWAA offers Airflow `3.3.1` in the region; otherwise set
  `airflow_version` to the version that the factory operators are tested against.

## 1. Deploy

```bash
uv run python -m factory_testing.sandbox steps          # the same order as below
scripts/sandbox/package-mwaa.sh                          # build/mwaa/plugins.zip
cd platform/sandbox
terraform init
terraform apply -var sandbox_account_id=$FACTORY_SANDBOX_ACCOUNT_ID -var 'admin_cidrs=["<your ip>/32"]' \
  -var budget_email=<you> -var mwaa_plugins_zip=../../build/mwaa/plugins.zip      # about 30-40 min
terraform output -json settings > /tmp/sbx-outputs.json
cd ../.. && scripts/build-runtime-image.sh "$(jq -r .ecr_repository_url /tmp/sbx-outputs.json)"   # prints the digest
uv run python -m factory_testing.sandbox settings --outputs /tmp/sbx-outputs.json --out /tmp/sbx-settings
```

Then point a sandbox copy of the team repo at `/tmp/sbx-settings`, choose the sandbox pipeline (open question 1 in
the Phase 8 review), and run `factory build <pipeline> --image <digest>` and `factory verify`. Then:

- apply `build/<pipeline>/terraform/dev`;
- upload `dags/dev/*.py` to `s3://<dags bucket>/dags/`;
- run `kubectl create namespace factory-clinops-data` and create the service accounts named in the runbook page of
  the build;
- in the Airflow UI, create the `team_clinops_data` role (spike S3 checks that it is limited to the team's DAGs).

## 2. Spikes (record each result in `docs/spikes/S<n>-sandbox.md`)

| Spike | Procedure | Pass |
|---|---|---|
| S3 trigger | Send the same message twice to the pipeline queue; time message → DAG run | second start answered 409; p95 latency recorded |
| S5 Pod Identity | From the t3 pod, try another pipeline's staging prefix; read CloudTrail for the pod's calls | access denied; CloudTrail shows namespace, service account and pod |
| S9 secrets backend | Put `airflow/connections/x` and an unrelated secret; read both from a task | the first resolves; the second is denied |
| S2 long pod | A pod task sleeping 20 minutes (EKS tokens last 15) | completes; or adopt the documented fallback |
| S6 10 GB | The synthetic 10 GB tree through t1 in Lambda (10 GB memory) and in a pod | within the memory bound; throughput feeds calibrated prices |
| S10 Fivetran | Only with a Fivetran sandbox key in the environment | connection created; no credential in Terraform state |

After the runs, write `pricing.json` (`status: calibrated`) from the measured durations and the Cost Explorer figures
for the day, and re-run `factory change plan --tf-plan dev=<terraform show -json>` against the live state.

## 3. Tear down (same day)

```bash
terraform -chdir=build/<pipeline>/terraform/dev destroy
terraform -chdir=platform/sandbox destroy -var ...                  # the same -var values as apply
```

Buckets and the ECR repository are created with force-destroy. The audit bucket's one-day GOVERNANCE lock needs
`s3:BypassGovernanceRetention`, or a wait of one day. Check the Billing console the next day.
