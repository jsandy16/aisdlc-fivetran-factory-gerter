# ADR-0021: GxP / Part 11 / ALCOA+ controls built into the workflow

- **Status:** Accepted
- **Date:** 2026-10-13
- **Amended:** PR approvals accepted as e-signatures (D5).

## Context

The pipelines move highly confidential pharma R&D data, some of it GxP-relevant. QA accepts GitHub PR approvals by SSO identities as Part 11 e-signatures (D5).

## Decision

- **Electronic signature:** PR + CODEOWNERS approval (one owner per spec file).
- **Custom-rule acceptance:** the acceptance digest is committed and signed by the rules owner's PR approval.
- **Audit records:** an agent-decision audit; CI deploy records; run manifests and evidence in Object Lock storage; portal destination resolutions audited.
- **Traceability** (requirement → rule → implementation → test → result) is generated in the review package.
- **GxP tier rules are enforced by the validator.** Encryption at rest is SSE-KMS; package encryption is deferred (D3).
- **Validation:** the framework is validated once (risk-based), with golden-corpus regression on upgrades.

## Consequences

- Evidence is a by-product of the workflow.

## Alternatives considered

- *An eQMS signature per change:* not required (D5).
