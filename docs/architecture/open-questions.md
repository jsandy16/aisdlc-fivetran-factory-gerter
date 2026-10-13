# Open questions

None of these blocks Phase 1. Each has a **fallback** that the design uses until it is answered.

| # | Question | Why it matters | Fallback |
|---|---|---|---|
| Q1 | **Worked example inputs:** what does the SQS message contain and who produces it (study id, portal entry id, S3 event)? One message per run, or does Task 1 drain the queue? Realistic max package size and file count? Are `<x>` and `<y>` in the same account and KMS key, and must both copies succeed together? | run_key, trigger mode, engine sizing, all-or-nothing delivery | one message per run (`request_id` → run_key), ≤ 50 GB / 20k files, same account and key, both prefixes required (the run fails if either fails; delivered copies stay, written once) |
| Q2 | **Encryption:** who decrypts the package and with what tool (AWS Encryption SDK, OpenPGP, raw AES-GCM)? Key source: KMS, Vault Transit or Vault KV? Is the MD5 over the ciphertext, the plaintext or both? | format and key custody; checksum semantics | ESDK + KMS; MD5 over the ciphertext (what is delivered); plaintext sha256 recorded in the manifest |
| Q3 | **Portal under GxP:** may portal path or schedule changes take effect at run time (`dynamic`), or must they go through a reviewed PR (`materialized`)? Who owns and versions the portal API contract? | governance of a runtime control plane | `dynamic` for non-GxP and gxp_indirect; `materialized` for gxp_direct |
| Q4 | **Part 11:** does QA accept SSO-backed GitHub PR approvals plus a digest-bound acceptance as electronic signatures? CSA or IQ/OQ/PQ? | signature mechanism; validation deliverables | PR approval + acceptance record; CSA-style framework validation |
| Q5 | **Sandbox:** is a dev AWS account available with MWAA 3.3.1, EKS, VPC endpoints and a reachable Vault? Which Vault (HCP or self-managed, Enterprise namespaces)? | AWS spikes S2, S3, S5, S9, S10; the Phase 7 real-run demo | everything mocked; AWS spikes deferred; Phase 7 demo runs locally |
| Q6 | **Account model:** `shared_per_env` or `account_per_team`? | placement, isolation strength, MWAA count | `shared_per_env` default; both generated |
| Q7 | **Workload identity default:** Pod Identity (recommended) or IRSA (named in the brief)? | IAM generation, ABAC | Pod Identity, with IRSA supported |
| Q8 | **Fivetran:** which plan tier, and is the Terraform provider allowed in the platform account? | `fivetran_native` engine path | `fivetran_sdk` (pf) path for governed sources; native connectors via the Terraform provider once allowed |
