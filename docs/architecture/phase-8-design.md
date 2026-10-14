# Phase 8 design: AWS validation in a sandbox account (approved plan of record)

Goal: the artefacts proven locally in Phase 7 deploy and run on real AWS (MWAA, Lambda, EKS), with synthetic data, in
a **sandbox account only**, and the remaining spikes (S2, S3, S5, S9, S10, S6 at 10 GB) get real answers.

## Status: blocked on sandbox credentials

The environment holds AWS credentials, but AWS rejects them (`InvalidClientTokenId`, checked read-only with
`sts:GetCallerIdentity`). Nothing was created in any account. Until valid sandbox credentials are added to the
environment settings, this phase prepares everything that does not need the account (A) and leaves the account work
(B) ready to run in one pass.

## A. Without an account (this session)

| # | Milestone | Proven by |
|---|---|---|
| A1 | Runtime image: one `Dockerfile` for Lambda (handler `factory_runtime.lambda_handler.handle`) and pods (`python -m factory_runtime.run`); base image pinned by digest | Dockerfile lint test; build happens in B (no Docker daemon here) |
| A2 | Sandbox platform stack `platform/sandbox/` (hand-written Terraform, as platform modules are): VPC with private subnets and endpoints, KMS keys and aliases, team stores, ECR, MWAA (DAG bucket, requirements with `factory_airflow`), EKS with the Pod Identity agent, the trigger role, a budget alarm | offline `terraform validate` + checkov with reviewed waivers |
| A3 | `python -m factory_testing.sandbox preflight`: identity, **allow-listed sandbox account only** (`FACTORY_SANDBOX_ACCOUNT_ID`), region, required services, an hourly cost estimate; `deploy` and `teardown` steps printed, never run without `--yes` | Stubber tests; refuses any other account |
| A4 | `factory change plan --tf-plan <env>=<plan.json>`: real create / update / **replace** / destroy from `terraform show -json` | synthetic plan JSON test |
| A5 | Sandbox runbook: credentials, cost, order, each spike's procedure and pass criterion, teardown | `docs/sandbox-runbook.md` |

## B. With a sandbox account (next session with credentials)

1. `preflight`, then apply `platform/sandbox/` (about 30–40 min, mostly MWAA and EKS).
2. Build and push the runtime image; render real platform settings from the stack outputs; `factory build`.
3. Apply the generated `dev` stack; publish the DAG; run the Phase 7 story with S3 destinations at x and y.
4. Spikes S3 (trigger, 409 on duplicates, latency), S5 (Pod Identity isolation, CloudTrail tags), S9 (MWAA secrets
   backend scope), S2 (pod > 15 min), S6 (10 GB stream), S10 (Fivetran, only with a Fivetran sandbox key).
5. Calibrated `pricing.json` from measured durations and the bill; `terraform plan -json` in change plans; tflint.
6. **Teardown** the same day (MWAA and EKS cost money per hour); the budget alarm is the backstop.

Safety: everything is refused unless the caller's account equals `FACTORY_SANDBOX_ACCOUNT_ID`; no real data; secrets
are created as synthetic values in the sandbox's Secrets Manager and never printed.

## As built (part A)

- **A1 image:** `packages/factory_runtime/image/Dockerfile` on the AWS Lambda Python 3.12 base pinned by digest;
  dependencies from `uv export --frozen` installed with `--require-hashes`, then the three workspace wheels. A clean
  install of exactly that set imports and runs the runtime here; `docker build` itself needs Docker (part B).
  `scripts/build-runtime-image.sh [ECR URI]` builds, pushes and prints the digest.
- **A2 sandbox stack:** `platform/sandbox/` (VPC with one NAT, S3/DynamoDB endpoints; KMS keys and the aliases the
  settings name; staging, bundle, audit (Object Lock, GOVERNANCE 1 day), DAG, landing and delivery buckets; run-state
  table with TTL; ECR; MWAA with `factory_airflow` from `plugins.zip` and the Secrets Manager backend scoped to
  `airflow/*`; EKS in API auth mode with the Pod Identity agent and a namespace-scoped access entry for MWAA; the
  trigger role; a budget). Offline `terraform validate` passes, and checkov passes with 14 reviewed sandbox-only waivers
  (`platform/sandbox/iac-waivers.yaml`; 4 are checkov not following `for_each` links).
- **A3** `python -m factory_testing.sandbox preflight | settings | steps`: preflight refuses without
  `FACTORY_SANDBOX_ACCOUNT_ID`, for any other account, and for unusable credentials, before anything but STS is called.
  `settings` renders real platform settings from `terraform output` and validates them (no Vault in the sandbox).
- **A4** `factory change plan --tf-plan ENV=FILE` uses `terraform show -json`: create / update / **replace** / destroy;
  replacing or destroying a stateful resource asks platform to approve.
- **A5** `docs/sandbox-runbook.md`: credentials, cost (about $0.82/hour always-on), order, spike procedures and pass
  criteria, teardown.
- **Not done (needs the account):** everything in B. tflint still not pinned (GitHub blocked here).
- **Tests:** 671 passed on 3.13 and 3.11; opt-in Airflow + IaC (now including the sandbox stack): 6 passed.
