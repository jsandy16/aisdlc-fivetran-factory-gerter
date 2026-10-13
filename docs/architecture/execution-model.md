# Execution model

## 1. Unit of execution

- **What runs.** An **orchestration task** is an ordered list of rule_ids, executed in **one engine invocation**:
  one pod, one Lambda invocation, or one MWAA control operator.
- **Inside a pod or Lambda.** The entry point runs:
  `python -m factory_runtime.run --bundle s3://<bundle-store>/<team>/<pipeline>/<sha256>.zip --sha256 <h> --task <task_id> --run-key <k>`.
  Lambda runs the same thing through `factory_runtime.lambda_handler.handle`.
- **The steps of a run:**
  1. Download the bundle and **verify its sha256** (the hash is embedded in the generated DAG or Lambda
     configuration). On a mismatch, fail with `security`.
  2. Load `spec.lock.json` and the environment binding.
  3. Read or create the run pin.
  4. Execute the task's rules in order. Each rule gets a `RuleContext`: typed inputs, secret *handles*, a workdir,
     a logger and run-state.
  5. Write the outputs and the checkpoint.
  6. Exit with the exit-code contract (section 6).
- **One image.** There is a single framework image per release, referenced by digest. Per-pipeline code (custom
  rules) travels in the **bundle**, a deterministic zip that is content-addressed and write-once. This avoids
  hundreds of per-pipeline image builds and scans ([ADR-0004](../adr/0004-execution-model.md)).

## 2. Run pinning

- **What is pinned.** The first task of a run writes `{bundle_sha, image_digest, logic_sha, binding_sha}` to
  run-state under `run_key`. Later tasks read the pin, not the current DAG file, so a deploy during a run cannot mix
  versions. The pod-operator `image` field is templated from the pin.
- **Breaking DAG-shape changes** get a new `dag_id` (`<pipeline_id>__v<major>`). The old DAG is retired: paused,
  drained, then deleted.
- **Retention.** Old bundles and images are kept for at least the retention period.

## 3. Artefacts and dataflow

| Kind | Example | Typical durability |
|---|---|---|
| `message_batch` | the SQS message (S3 package metadata: bucket, prefix, …) that triggered the run | staged (custody snapshot) |
| `portal_snapshot` | portal response for this run | staged, write-once, hashed |
| `file_set` | downloaded files with relative paths | local or stream |
| `package` | zip / tar.gz preserving folder structure | stream → staged |
| `manifest` | path, size, sha256 (and md5 if asked) per file + package checksums | staged + final |
| `checksum` | md5 / sha256 of a stored object | value + sidecar |
| `value` | small typed value (count, name) | XCom-safe reference |
| `report` | validation / test report | staged + audit |

**Durability levels:**
- `stream`: never materialised.
- `local`: pod or Lambda ephemeral disk.
- `staged`: `s3://<staging, team account>/<pipeline>/<run_key>/<artifact>` with SSE-KMS (team key), lifecycle by tag:
  7 days after success, and at least the replay window after a failure.
- `final`: a declared target.

**Validator rules:**
- Every input has exactly one upstream producer.
- Anything crossing a task boundary is `staged`.
- Only `upload`/`copy` ops write `final` artefacts, and only to declared targets.

**What Airflow sees.** XCom and `dag_run.conf` carry only ids, URIs and hashes. They are visible in the Airflow UI,
so they never hold secrets, paths or payloads.

## 4. Run keys and runtime idempotency

| Trigger | `run_key` |
|---|---|
| schedule | pipeline_id + data interval start |
| s3_event | bucket + key + versionId (or sequencer) |
| sqs | message attribute declared in `reliability.run_key` (e.g. request id), else the MessageId |
| portal_schedule | portal entry id + schedule slot |
| manual | required in conf (`run_key`), validated |

- **Duplicate triggers.** `run_key` (sanitised) is used as the Airflow `dag_run_id`, so Airflow rejects a duplicate
  trigger, and the dispatcher treats HTTP 409 as "already running".
- **Writes.** Every target write is `PutObject` / `CompleteMultipartUpload` with `If-None-Match: *`. On 412 the
  existing object's checksum is compared:
  - **equal** means success;
  - **different** on a nondeterministic op (e.g. encryption, when added) means *adopt the winner*: downstream steps use the stored
    object;
  - any other difference is a `data` error.
- **Sequence numbers.** `{seq}` is allocated transactionally in run-state: a counter `ADD` plus a conditional put of
  `run_key → seq`. Retries reuse the number. Gaps are possible and documented.
- **Checkpoints.** Per task and rule, in run-state. A completed `run_key` + task is a no-op on rerun.
- **Forced rerun.** It creates `{rev}` = n+1 and a `supersedes` record, and needs approval. Targets are never
  overwritten.
- **Portal input.** The portal response is snapshotted once per run, and every retry reads the snapshot.

## 5. Large packages

- **Streaming path.** Source `open_stream()` → deterministic zip writer (sorted entries, fixed timestamps and modes,
  zip64, data descriptors, `store` for already-compressed members) → S3 multipart upload. Package encryption is
  deferred (D3); when it is added, it becomes one more stream stage.
  - Part size adapts from 64 MiB so that 10,000 parts cover `workload.bytes.max`.
  - Memory ≈ part size × upload concurrency.
- **Checksums.** MD5 and SHA-256 of the zip are computed **while streaming**. A separate checksum
  step re-reads the **stored** object for an independent check.
  - The S3 ETag is not an MD5 for SSE-KMS or multipart objects.
  - Checksums cannot go into multipart object metadata, so they are written as sidecar objects and into the manifest.
- **Size.** Max 10 GB per run (D10). Deflate runs at roughly 50–100 MB/s per core. The planner sizes pods and sets
  `activeDeadlineSeconds` from `workload`.
- **Fused tasks.** When a split layout would re-download a large package between tasks, the planner suggests one
  fused task. It warns; it never changes task boundaries itself.

## 6. Error taxonomy, exit codes and alert routing

| Class | Examples | Exit code | Airflow behaviour | Default route |
|---|---|---|---|---|
| ok | — | 0 | success | — |
| transient | network reset, throttling, 5xx/429 after retries, Vault unavailable | 75 (EX_TEMPFAIL) | retry per task policy | none until retries are exhausted |
| data | validation failed, checksum mismatch, empty package, manifest mismatch | 65 (EX_DATAERR) | fail, no retry | data owner (SNS / email / Slack) |
| config | missing secret, wrong binding, auth refused, schema mismatch from the portal | 78 (EX_CONFIG) | fail, no retry | platform / data engineer |
| security | bundle hash mismatch, host-key mismatch, destination not in the allow-list, untrusted certificate | 77 (EX_NOPERM) | fail, no retry, page | security |

`factory_airflow`'s pod-operator subclass maps exit codes to `AirflowFailException` (no retry) for every class
except transient. Messages never contain data values or secrets (the pf 0.1.0 convention).

## 7. Structured logging

Every component writes JSON lines:

```json
{"ts":"2026-10-13T10:00:00.000Z","level":"INFO","event":"rule_end","pipeline_id":"study_pkg_delivery",
 "team":"clinops_data","env":"prod","run_id":"<airflow dag_run_id>","run_key":"<sha-prefix>","task_id":"t2_encrypt",
 "rule_id":"r_encrypt","correlation_id":"<uuid from trigger>","attempt":1,"duration_ms":81234,
 "bytes_in":21474836480,"bytes_out":21474900000,"error_class":null,"bundle_sha":"<sha>","image_digest":"<digest>"}
```

- **Never logged:** secret values (a redaction filter masks every revealed value), data values, classified portal
  payloads, full server paths at `restricted` (a hashed form is logged instead).
- **Where logs go.** Pods log to their team's CloudWatch log group. MWAA task logs only reference the pod
  (`get_logs=False`).
- **Metrics.** CloudWatch metrics are derived through embedded metric format:
  `RunDuration`, `BytesTransferred`, `FilesPackaged`, `RuleFailures{class}`, `RetryCount`, `SecretAccess`,
  `PortalLatency`. Alarms are generated per pipeline.
- **Lineage.** Per-run lineage, from source file to S3 object, comes from the manifest, which is copied to the audit
  store. OpenLineage dataset names are optional (`lineage` section).
