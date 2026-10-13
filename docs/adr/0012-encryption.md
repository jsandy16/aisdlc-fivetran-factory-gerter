# ADR-0012: Package encryption: deferred (done outside the pipeline); design kept for later

- **Status:** Deferred
- **Date:** 2026-10-13
- **Amended:** deferred by user decision D3.

## Context

The user decided that package encryption is done outside the pipeline for now and will be added later (D3).

## Decision

- The `encrypt` rule category and the ops `crypto.encrypt` / `crypto.decrypt` stay in the schema with `status: planned`. Using them gives `op_not_available`.
- `security.package_encryption` accepts only `none` for now.
- **Data at rest is still encrypted:** SSE-KMS on staging, audit and S3 destinations (the GxP validator requires it). Transit is TLS.
- **When it is re-opened**, the earlier proposal is the starting point:
  - AWS Encryption SDK with a KMS keyring (or Vault Transit / Vault KV raw keys), streaming, frames ≥ 1 MiB;
  - OpenPGP as an alternative format;
  - a decrypt-verify step for gxp_direct.

## Consequences

- The worked example has 3 tasks; adding encryption later is an incremental change (new rule + task), handled by the change engine.

## Alternatives considered

- *Building encryption now:* not needed (D3).
