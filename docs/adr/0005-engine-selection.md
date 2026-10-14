# ADR-0005: Spec-driven engine selection by a deterministic planner

- **Status:** Accepted
- **Date:** 2026-10-13

## Context

The brief requires a documented decision matrix and no hard-coded engine choices. Fivetran can run Connector SDK Python, but only to emit rows into a Fivetran destination.

## Decision

- **Engines:** `fivetran_native`, `fivetran_sdk` (pf), `lambda`, `mwaa` (control only), `eks_pod` on-demand and spot. MWAA Serverless was evaluated and not adopted.
- **Planner steps:**
  1. Intersect the ops' `allowed_engines`.
  2. Apply policy filters (MWAA control-only; spot only if idempotent and checkpointed; not for gxp_direct).
  3. Estimate from `workload` and the op resource models.
  4. Check feasibility (Lambda ≤ 900 s with a 2× margin, ≤ 10 GB memory and /tmp).
  5. Pick the cheapest monthly cost, with deterministic tie-breaks.
  6. Record `engine_decisions[]`.
- **Overrides.** An explicit engine wins if feasible. The planner never changes task boundaries; it only warns (`fuse_recommended`).
- **Versioned constants.** `engine_table.v1.json` and `pricing.v1.json` are versioned and part of the build hash.

## Consequences

- Every engine choice is explainable and reproducible.
- Estimates depend on the quality of the `workload` hints. Spike S6 calibrates the throughput figures.

## Alternatives considered

- *LLM chooses the engine:* rejected, because it is not reproducible.
- *A fixed cost order (Lambda < Fivetran < MWAA < EKS):* replaced by per-task estimates.

## Phase 3 amendments

- Planner in `factory/plan/engine.py`; placeholder prices in `factory_spec/catalog/pricing.v1.json` (decimal strings). Decisions are printed by `factory plan`, never written to the lock (P3-8).
