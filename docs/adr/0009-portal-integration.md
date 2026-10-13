# ADR-0009: Portal as a first-class spec section; JMESPath; runtime-effective; returns destinations bounded by an allow-list

- **Status:** Accepted
- **Date:** 2026-10-13
- **Amended:** dynamic only (D4); the portal returns destinations (D2).

## Context

The portal returns, per study or package, where the package must be delivered: another S3 bucket/prefix or an external server (D2). Portal changes take effect immediately (D4).

## Decision

- **Spec section `portal`:**
  - auth: oauth2 client credentials / API key / mTLS via secret refs;
  - endpoints with templated paths (declared variables only) and pagination (none/page/cursor/link-header);
  - `response` field specs, from which a deterministic Pydantic model is generated (fail fast);
  - `mapping` in **JMESPath only** (destinations per slot, values, `expect` guards);
  - reliability (timeouts, full-jitter backoff honouring `Retry-After`, a per-process breaker and rate limit, a portal pool);
  - a write-once snapshot per run.
- **Portal output takes effect at run time.** It is bounded by the destination allow-list ([ADR-0024](0024-portal-resolved-destinations.md)).
- **Portal calls run in team-account Lambdas or pods**, never on MWAA.

## Consequences

- Portal changes need no PR, but cannot send data anywhere that governance has not approved.
- Retries see the same input.

## Alternatives considered

- *A PR-gated ("materialized") portal:* dropped by user decision D4.
- *JSONPath:* implementations differ.
