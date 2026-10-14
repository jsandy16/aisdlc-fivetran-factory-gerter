# Phase 7 design: local end-to-end demo and one incremental change (approved plan of record, offline)

No AWS (D6): moto, the TLS stand-ins (portal, Vault), the SFTP stand-in. Synthetic data and synthetic secrets only.

| # | Milestone | Proven by |
|---|---|---|
| M0 | Live agent recordings if `ANTHROPIC_API_KEY` is in the environment (otherwise skipped and reported) | recordings with real provenance |
| M1 | `python -m factory_testing.demo --out DIR`: copy the worked example into a scratch git repo; `validate` → `test` → `build` → `verify`; start the stand-ins; dispatch a message with the dispatcher configured from the **generated Terraform**; run the task commands of the **generated DAG** in order; collect the evidence (files at x and y, run outcomes, audit, receipts, docs); rerun = no-op | the demo test passes; `DEMO-REPORT.md` |
| M2 | Change 1 (non-breaking), request → spec edit → `change plan --record` → rebuild → `verify` → commit → new run | same DAG; only the `.sha256` sidecar is delivered |
| M3 | Change 2 (breaking), refused without a major bump, then `2.0.0` → new `__v2` DAG; the dispatcher starts the new DAG | delivered by `study_pkg_delivery__v2` |
| M4 | Demo write-up (`docs/demo.md`): what to show, in what order, and what each step proves | — |

Defaults agreed: the demo is a dev-only module (`factory_testing`), driven through the real CLI and the generated
artefacts, with no hand-written run commands. Re-accepting a custom rule is not part of this demo because the worked
example has no custom rules; the Phase 4 tests cover that path.

## As built

- **M0 not done:** still no `ANTHROPIC_API_KEY` in this environment.
- **Demo:** `factory_testing/demo.py` (14 steps, about 20 s; `test_stand_demo.py` runs it in the suite). It configures
  the runtime from the generated Terraform's Lambda environment (`from_environ`) and the pod from the DAG's `env_vars`,
  uploads the built bundle to the URI the generated config names, and renders the DAG's Airflow templates.
- **Found and fixed by the demo:**
  - Pod tasks received no `FACTORY_*` settings: on EKS, t3 would have exited 78. The DAG generator now passes the same
    non-secret settings Terraform gives the Lambdas (`terraform.runtime_env`) as the pod's `env_vars`.
  - `change plan` on an invalid spec crashed with a traceback; it now returns the validation errors in the envelope.
- **Change 1 needed governance:** dropping MD5 sidecars also changes `security.yaml` (the validator refused the
  rules-only edit with `checksum_policy_mismatch`). The plan asks governance to approve.
- **Change 2:** a `run_key` change is refused at 0.3.0 and accepted at 2.0.0, producing `study_pkg_delivery__v2`.
- **Tests:** 664 passed on 3.13 and 3.11; opt-in Airflow + IaC (with pod `env_vars`): 5 passed.
