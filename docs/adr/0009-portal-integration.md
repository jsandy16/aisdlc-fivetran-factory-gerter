# ADR-0009: Portal as a first-class spec section; JMESPath; untrusted paths; dispatcher Lambda; dynamic or materialized mode

- **Status:** Proposed (Phase 1 review)
- **Date:** 2026-10-13

## Context

The portal returns server paths, path types, include/exclude patterns, metadata and per-entry schedules. It is a runtime control input that could redirect downloads if trusted blindly.

## Decision

- **Spec section `portal`:**
  - auth: OAuth2 client credentials / API key / mTLS via secret refs;
  - endpoints with templating from an allow-listed token set, plus pagination (none/page/cursor/link-header);
  - a `response_schema` that becomes a generated Pydantic model (fail fast);
  - `response_mapping` in **JMESPath only**;
  - reliability (timeouts, full-jitter backoff honouring `Retry-After`, a per-process breaker and rate limit, a portal pool);
  - a write-once snapshot per run.
- **Paths are untrusted.** They are canonicalised and must sit under `allowed_roots`. The host always comes from the spec.
- **Portal calls never run on MWAA.** Per-entry schedules use a dispatcher Lambda: snapshot → due entries → `InvokeRestApi` with `dag_run_id = run_key`.
- **Modes:** `dynamic`, or `materialized` (changes go through a PR), recommended for gxp_direct.

## Consequences

- Portal changes cannot cause downloads from unexpected hosts or paths.
- Retries see the same input.
- The mock portal is generated from the schema.

## Alternatives considered

- *JSONPath:* rejected, because implementations differ.
- *A distributed DynamoDB rate limiter:* rejected, because of hot keys.
- *Calling the portal from MWAA:* rejected, because of credentials on the shared role.
