# Open questions

## Answered (user, 2026-10-13)

| # | Question | Answer | Recorded as |
|---|---|---|---|
| Q1 | Worked example inputs | The SQS message contains metadata about a package uploaded to S3 (bucket, prefix, …). The portal path is the destination: another S3 bucket or an external server (HTTPS, …). Max package 10 GB. | D1, D2, D10 |
| Q2 | Encryption | Done outside the pipeline for now; added later. | D3, ADR-0012 (deferred) |
| Q3 | Portal under GxP | Changes take effect immediately. | D4, ADR-0009 |
| Q4 | Part 11 | GitHub PR approvals are accepted as e-signatures. | D5, ADR-0021 |
| Q5 | Sandbox | Not now; available when the framework is complete. | D6, Phase 8 |
| Q6 | Account model | One account per team per environment (dev, test, ppt, prod). MWAA central; EKS and data in team accounts. | D7, ADR-0015, ADR-0006 |
| Q7 | Workload identity | User unsure; we chose Pod Identity (IRSA supported). | D8, ADR-0011 |
| Q8 | Fivetran | SaaS. | D9 (REST API; Terraform provider optional) |

## Still open (each has a fallback the design uses)

| # | Question | Fallback |
|---|---|---|
| Q9 | Must the `<x>` and `<y>` deliveries succeed together (all-or-nothing)? | Both are required. The run fails if either fails; delivered copies stay (write-once), and a retry completes the missing one idempotently. |
| Q10 | May the Fivetran Terraform provider be used for native connectors? | REST API through the pf `fivetran_api` pattern. |
| Q11 | Which other destination types beyond S3, SFTP and HTTPS ("any other destination")? | S3, SFTP and HTTPS in v1; the allow-list union is extensible (FTPS, SMB, … later). |
| Q12 | Does the portal API publish an OpenAPI/JSON Schema contract? | The contract is captured as `response` field specs in the spec; the mock is generated from it. |
