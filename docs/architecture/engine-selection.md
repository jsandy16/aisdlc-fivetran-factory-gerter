# Engine selection: decision matrix and planner

Engine selection is driven by the spec, never hard-coded. Each task's `engine:` is either `auto` (the planner
decides) or explicit (it must pass the feasibility check). The planner is **deterministic code, not an LLM**. Its
constants live in versioned files (`plan/engine_table.v1.json`, `cost/pricing.v1.json`) that are part of the build
hash ([ADR-0005](../adr/0005-engine-selection.md)).

## 1. Decision matrix

| Engine | Use for | Hard limits | Custom code | Concurrency | Cost profile | Start latency |
|---|---|---|---|---|---|---|
| `fivetran_native` | standard DB / SaaS sources → lake tables | tables only; Fivetran owns layout (no prefix templates, packaging, object tags) | none | 1 sync per connection | MAR (monthly active rows); spikes on churn or re-sync | sync interval |
| `fivetran_sdk` (pf 0.1.0) | governed file/DB sources → Iceberg tables with row/package checks | row-oriented; Fivetran runtime; no object delivery | yes, rows only (pf_runtime, CEL) | 1 sync per connection | MAR + Fivetran compute | sync interval |
| `lambda` | dispatch, SQS consume, portal snapshot, server-side copy/rename, small checksum, small transforms | 15 min, 10 GB memory, 10 GB `/tmp`, 6 MB sync payload | yes (bundle) | account-shared; reserved concurrency per function | per GB-second; ~0 idle | ms; seconds for image cold start |
| `mwaa` (control only) | branch, trigger, deferrable waits, Fivetran sync trigger, pod/Lambda launch | **no data, no secrets** | `factory_airflow` only | pools per team | fixed environment cost, amortised | scheduler loop (seconds) |
| `eks_pod` on-demand | large downloads, zip/tar, encryption, bulk transfer; long runs; `gxp_direct` | cluster capacity, namespace quota | yes (bundle) | namespace quota + pool | per vCPU-second + memory; cheapest above ~10 min of work | 10 s – 2 min (+ node provisioning) |
| `eks_pod` spot | the same, when restartable and checkpointed, ≤ ~60 min | 2-minute interruption notice | yes | same | ~60–70% below on-demand | same |
| MWAA Serverless | **evaluated, not adopted**: Amazon-provider operators only; per-task billing ($0.08/h, us-east-1) | operator allow-list | no | managed | per task | — |

**Rule of thumb from the brief, as refined.** For equivalent small work, Lambda is cheapest. MWAA cost is fixed and
shared, so MWAA tasks cost little at the margin but must not do data work. Fivetran fits only tables. EKS fits
anything heavy. The planner compares estimates rather than relying on a fixed order.

## 2. Planner algorithm (per task)

1. **Pipeline level.** A tabular source with only row/column rules and a lake-table target → `fivetran_native`.
   Custom row logic → `fivetran_sdk`. Everything else is planned per task.
2. **Candidates.** The intersection of `allowed_engines` across the task's ops ([op-catalog.md](op-catalog.md)).
3. **Policy filters**, recorded when they fire:
   - `mwaa` only for control ops (no `reads_source`, `reads_secret`, `writes_*` side effects);
   - `eks_pod_spot` only if every op is idempotent and the task is checkpointed;
   - `gxp_direct` excludes spot;
   - a classification at or above `restricted` excludes engines without a team-scoped role.
4. **Estimates.** From `workload` (p95 and max), each op's resource model and throughput: seconds, peak memory,
   disk and egress bytes per engine.
5. **Feasibility.**
   - `lambda` if `2 × est_seconds(max) ≤ 900`, memory ≤ 10 GB, and disk ≤ 10 GB or every op streams.
   - `eks_pod_spot` if `est_seconds(p95) ≤ 3600`.
   - `eks_pod` is always feasible and is sized from the estimate (requests and limits, ephemeral storage,
     `activeDeadlineSeconds`).
6. **Choice.** The lowest monthly cost (`runs_per_month × unit_cost`). Ties break in a fixed order: `lambda` <
   `mwaa` < `eks_pod_spot` < `eks_pod`.
7. **Explicit overrides.** They win if feasible; otherwise they are an error. The planner never changes task
   boundaries; it only warns (e.g. `fuse_recommended` when tasks would re-transfer a large package).
8. **Output.** `engine_decisions[]` in the lock and the docs: inputs, candidates, filters that fired, estimates and
   costs. Every choice can be explained.

## 3. The worked example under the planner

Workload `bytes max = 50 GB`, `p95 = 20 GB`, 4 runs/day, `gxp_tier: gxp_indirect`:

| Task | Ops | Planner result | Why |
|---|---|---|---|
| t1_collect | sqs.read, portal.lookup, discover.list, transfer.download, package.zip, manifest.build | `eks_pod` on-demand | 50 GB is too much for Lambda's /tmp and time; GxP excludes spot |
| t2_encrypt | crypto.encrypt | `eks_pod` on-demand + warning `fuse_recommended` (re-reads 50 GB from staging) | streaming encryption of 50 GB takes about 10–20 min |
| t3_checksum | checksum.compute (md5) | `eks_pod` | Lambda's single-stream S3 read is about 90 MB/s (resource-model value, measured in spike S6), so 50 GB takes about 555 s. With the 2× margin that is ~1,110 s, over the 900 s limit. Lambda becomes feasible if `max` is ≤ ~40 GB. |
| t4_upload | s3.copy ×2 (server-side) | `lambda` | server-side copy moves no bytes through the function |
