# ADR-0021: GxP / Part 11 / ALCOA+ controls built into the workflow

- **Status:** Proposed (Phase 1 review)
- **Date:** 2026-10-13

## Context

The pipelines move highly confidential pharma R&D data, so some are GxP-relevant.

## Decision

- **Approvals:** PR + CODEOWNERS (one owner per spec file), plus digest-bound person acceptance for custom rules.
- **Audit records:** an agent-decision audit; CI deploy records; run manifests and evidence in Object Lock storage.
- **Traceability** (requirement → rule → implementation → test → result) is generated in the review package.
- **GxP tier rules are enforced by the validator.**
- **Validation:** the framework is validated once (risk-based), with golden-corpus regression on upgrades.

## Consequences

- Evidence is a by-product of the workflow, not extra paperwork.
- QA must confirm the signature approach (Q4).

## Alternatives considered

- *Manual evidence collection per pipeline:* rejected, because it does not scale.
