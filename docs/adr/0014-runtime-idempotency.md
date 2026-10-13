# ADR-0014: Deterministic run keys, write-once targets, transactional sequence numbers, revisions for forced reruns

- **Status:** Accepted
- **Date:** 2026-10-13

## Context

Runs must be safe to rerun. SQS delivers at least once. Airflow 3 can leave `logical_date` empty for API- and asset-triggered runs.

## Decision

- **`run_key`** is derived per trigger type:
  - schedule: the data interval;
  - S3 event: bucket + key + versionId;
  - SQS: the declared message attribute or the MessageId;
  - portal: entry + slot;
  - manual: required.
- **`run_key` is the Airflow `dag_run_id`**, so Airflow rejects duplicate triggers.
- **Targets are written once** (`If-None-Match: *`). On 412 the checksum is compared: equal means success; different on a nondeterministic op means adopt the winner; anything else is a data error.
- **`{seq}`** is allocated transactionally per `run_key`. Gaps are possible.
- **Checkpoints** are kept per task and rule in run-state.
- **A forced rerun** creates `{rev}` and a supersedes record, and needs approval. Targets are never overwritten.

## Consequences

- Exactly-once outcomes are possible on at-least-once infrastructure.
- Package names are deterministic per run key.

## Alternatives considered

- *Using `logical_date` as the key:* rejected, because it can be null in Airflow 3.
