# ADR-0023: JSON logs with correlation ids, embedded metrics, error-class alert routing

- **Status:** Accepted
- **Date:** 2026-10-13

## Context

Every error must be classified and routed. Runs must be traceable from source file to S3 object.

## Decision

- **Logs:** JSON lines with `pipeline_id`, `team`, `env`, `run_id`, `run_key`, `task_id`, `rule_id`, `correlation_id`, `attempt`, `bundle_sha`, `image_digest` and `error_class`. A redaction filter applies.
- **Metrics:** CloudWatch embedded metric format, with generated per-pipeline alarms.
- **Alert routing:** routes per error class (transient/data/config/security) from the `observability` section to SNS, email or Slack.
- **Lineage:** per-run lineage comes from the manifest in the audit store; OpenLineage is optional.

## Consequences

- Uniform troubleshooting across engines.
- The runbook in the docs is generated from the same fields.

## Alternatives considered

- *Engine-specific logging:* rejected.

## Phase 3 amendments

- Third-party loggers clamped to WARNING; logs carry the run key as a hash only; delivery targets appear hashed in logs and in full only in the access-controlled audit store (`<pipeline>/<run_dir>/<task>/attempt-<n>.jsonl`).
