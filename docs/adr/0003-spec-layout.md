# ADR-0003: Spec as six owner-scoped files merged into one model; logic separated from environment bindings

- **Status:** Accepted
- **Date:** 2026-10-13
- **Amended:** 6th file `destinations.yaml` (governance); classification moved to `security.yaml`; environments from `factory.toml`.

## Context

Governance needs one approver per decision area (as pf proved with six files). BAs need a section-by-section experience. GxP needs proof that what was tested is what runs in production. Destinations chosen by the portal must be bounded by a reviewed list that governance approves (D2).

## Decision

- **Six files** under `spec/`, merged into one `PipelineSpec` (Pydantic v2, `extra=forbid`), each with exactly one CODEOWNER:

  | File | Owner | Hash |
  |---|---|---|
  | `pipeline.yaml` | data engineer | logic |
  | `rules.yaml` | BA / product owner | logic |
  | `integrations.yaml` | data engineer | logic |
  | `security.yaml` (incl. data classification) | governance / QA | logic |
  | `environments.yaml` | platform | binding |
  | `destinations.yaml` (destination allow-list per environment) | governance | binding |

- **Hashes.**
  - `logic_sha` covers the four logic files; it excludes cosmetic metadata (names, owners, labels, spec_version).
  - `binding_sha[env]` covers that environment's slice of the two binding files.
- **Environments.** The environment list comes from the team repo's `factory.toml` (dev, test, ppt, prod), and
  every environment must be bound.
- **References** between files use stable ids only.
- **Lock.** `spec.lock.json` is the canonical normalised merge.
- **Derived, not authored:** `infrastructure`, `artifacts` and the documentation output.
- **Versioning:** `schema_version` and `spec_version` are semver, with chained migrations.

## Consequences

- The same logic hash in dev, test, ppt and prod is GxP evidence.
- Destination approvals are a governance signature.
- BAs use `factory show --view ba` and the intake agent rather than raw files.

## Alternatives considered

- *A single file:* no per-section ownership.
- *The brief's 15 sections as 15 files:* too fragmented.
- *The allow-list inside `environments.yaml`:* the wrong owner (platform), and CODEOWNERS cannot split one file by key.
