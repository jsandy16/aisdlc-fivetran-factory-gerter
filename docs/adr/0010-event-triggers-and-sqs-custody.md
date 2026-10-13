# ADR-0010: Events via Lambda dispatch or in-task custody transfer; no AssetWatcher for GxP custody

- **Status:** Proposed (Phase 1 review)
- **Date:** 2026-10-13

## Context

S3 notifications only target standard SQS queues, so messages can be duplicated and arrive out of order. Airflow's AssetWatcher with `MessageQueueTrigger` deletes the message when it fires and may merge events into one run.

## Decision

- **`trigger.type: sqs | s3_event` supports two modes:**
  - **`dispatch` (default):** a Lambda event source mapping (partial batch response, DLQ, `maxReceiveCount`) writes a write-once message snapshot to staging, records a dedupe key in run-state, and calls MWAA `InvokeRestApi` with `dag_run_id = run_key`. A 409 means a duplicate. Task 1's `sqs.read` reads the snapshot.
  - **`in_task`:** the DAG is scheduled. `sqs.read` receives with a visibility timeout longer than the task (or extends it), snapshots write-once, writes the dedupe record, and then deletes. Retries read the snapshot, never the queue.
- **EventBridge** is used where fan-out to several consumers is needed.

## Consequences

- No message is lost or processed twice.
- Event-driven by default, without polling sensors.

## Alternatives considered

- *AssetWatcher SQS scheduling:* allowed only for non-GxP pipelines that accept its semantics.
