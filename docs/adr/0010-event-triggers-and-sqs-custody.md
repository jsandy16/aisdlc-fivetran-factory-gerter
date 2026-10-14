# ADR-0010: S3 event → SQS → dispatch Lambda (team account) → cross-account InvokeRestApi; custody snapshot; no AssetWatcher for GxP custody

- **Status:** Accepted
- **Date:** 2026-10-13
- **Amended:** cross-account trigger path (D7); message = S3 package metadata (D1).

## Context

A package is uploaded to S3 in the team account, and its metadata (bucket, prefix, …) arrives on SQS (D1). S3 notifications only target standard queues, so messages can be duplicated and arrive out of order. Airflow's AssetWatcher deletes the message when it fires and may merge events.

## Decision

- **`trigger.type: sqs`, `mode: dispatch` (default):**
  - A Lambda event source mapping in the team account (partial batch response, DLQ, `maxReceiveCount`) validates the message against the spec's message schema.
  - It writes a write-once message snapshot to staging and records a dedupe key in run-state.
  - It assumes `factory-trigger-<team>` in the platform account and calls MWAA `InvokeRestApi` as the team's Airflow RBAC role with `dag_run_id = run_key`. A 409 means a duplicate.
  - Task 1's `sqs.read` reads the snapshot.
- **`mode: in_task`:** the DAG is scheduled; `sqs.read` receives, snapshots, dedupes, then deletes.
- **EventBridge** is used for fan-out to several consumers.

## Consequences

- No message is lost or processed twice.
- Event-driven by default.
- The cross-account trigger path is validated in Phase 8 (spike S3).

## Alternatives considered

- *AssetWatcher SQS scheduling:* only for non-GxP pipelines that accept its semantics.

## Phase 3 amendments

- Dispatcher states `received → snapshotted → triggered` in run-state (`DISP`); body-only custody snapshot; per-message receipt; poison messages quarantined in staging and acknowledged; only transient failures go to `batchItemFailures`.
- `s3_event`: one run per created object; `s3.list` lists exactly that object (prefix filters and the S3 test event handled).
