# ADR-0019: LLM agents author; deterministic code decides; orchestrator is a state machine with gates

- **Status:** Proposed (Phase 1 review)
- **Date:** 2026-10-13

## Context

The brief lists 11 agents and strict gates. pf proved isolated Agent SDK sessions with record/replay. The same model must not grade its own work.

## Decision

- **LLM agents:** Intake (typed spec patches + open questions), Rule Implementation, Rule Review (separate context), Test (edge cases only; BA `test_cases` are the oracle), Change intake.
- **Deterministic code:** validator, generators, IaC, cost, docs, change planner, orchestrator.
- **`AgentRunner`** generalises pf's isolated session. It keeps record/replay, uses a logical model id, and has no file, network or credential access.
- **Self-correction:** at most 3 attempts per agent, stopping early on a repeated error signature, within a budget; then a typed `Escalation`.
- **Audit:** every decision goes to the committed audit JSONL.
- **Submission:** the review package is submitted only when every gate passes.

## Consequences

- Gates are reproducible.
- Agent output is always a reviewable typed artefact.

## Alternatives considered

- *An LLM orchestrator:* rejected, because it is not deterministic and its gates could be talked around.
