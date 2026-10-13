# ADR-0001: Evolve pipeline-factory 0.1.0 into a multi-engine framework; freeze pf

- **Status:** Accepted
- **Date:** 2026-10-13

## Context

pf 0.1.0 is a proven, tested Fivetran Connector SDK factory (270 tests, dry run 91/91). The new requirements need MWAA, EKS, Lambda, packaging, encryption and a generic rule-based spec. That is much broader than pf's row-oriented, Fivetran-only model, whose source dispatch is hard-coded in about nine places.

## Decision

- **pf is frozen.** `pf` and `pf_runtime` stay in `src/`, unchanged, and become the backend of the `fivetran_sdk` engine through a factory spec → pf lock adapter (Phase 5).
- **New packages go in a uv workspace under `packages/`:** `factory_core`, `factory_spec`, `factory_runtime`, `factory_airflow`, `factory`.
- **pf code is adapted, not imported.** Proven pf code (retry, errors, safe unzip, write-once S3, sources, CEL, yamlio, canonical, envelope, LLM record/replay, AgentRunner basis, acceptance digest, hooks) is copied in adapted form into the new packages ([reuse.md](../architecture/reuse.md)).
- **pf's suite and dry run stay green in CI** in every phase.

## Consequences

- No regression risk for existing Fivetran pipelines. Two spec dialects exist until a later `factory import-pf` migrates pf pipelines; authoring new pipelines uses only the factory spec.
- Some code is duplicated between `pf_runtime` and `factory_core` by design. It is consolidated only in a deliberate later refactor.

## Alternatives considered

- *Rewrite pf in place:* rejected, because it puts a validated tool at risk for no benefit.
- *A new repository from scratch:* rejected, because it loses the history and the proven controls.
