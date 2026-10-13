# ADR-0003: Spec as owner-scoped files merged into one model; logic separated from environment bindings

- **Status:** Proposed (Phase 1 review)
- **Date:** 2026-10-13

## Context

Governance needs one approver per decision area (as pf proved with six files). BAs need a section-by-section experience. GxP needs proof that what was tested is what runs in production.

## Decision

- **Five files** under `spec/`, merged into one `PipelineSpec` (Pydantic v2, `extra=forbid`), with CODEOWNERS listing exactly one owner per file:
  - `pipeline.yaml` (data engineer);
  - `rules.yaml` (BA / product owner);
  - `integrations.yaml` (data engineer);
  - `security.yaml` (governance / QA);
  - `environments.yaml` (platform).
- **Stable ids only** for cross-references.
- **The lock.** `spec.lock.json` is the canonical normalised merge.
- **Hashes.** `logic_sha` covers everything except `environments.yaml`; there is a `binding_sha` per environment.
- **Derived sections:** `infrastructure`, `artifacts` and the documentation output are derived, not authored.
- **Versioning:** `schema_version` and `spec_version` are semver, with chained migrations.

## Consequences

- The same logic hash in dev, test and prod is GxP evidence.
- BAs use `factory show --view ba` and the intake agent rather than raw files.
- Five files are more to navigate. The loader and agent hide this.

## Alternatives considered

- *A single file:* rejected, because it gives no per-section ownership.
- *The brief's 15 sections as 15 files:* rejected, because it fragments the spec too much.
- *A separate `infrastructure.yaml`:* rejected, because infrastructure is derived from platform settings.
