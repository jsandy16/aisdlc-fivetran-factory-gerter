# ADR-0015: AWS account model as a platform setting supporting shared-per-env and account-per-team

- **Status:** Proposed (Phase 1 review)
- **Date:** 2026-10-13

## Context

The organisation has not decided the account model.

## Decision

- `platform-settings/<stage>.json` declares `account_model: shared_per_env | account_per_team`, the account map and placement.
- Generators emit provider aliases, cross-account bucket and KMS key policies, state keys and role ARNs from the settings.
- Specs never contain account ids. Their `environments.yaml` names placement aliases only.
- The default is `shared_per_env`. Both models are tested in Phase 5.

## Consequences

- The decision can be made later without changing pipeline specs.
- The generator is a little more complex.

## Alternatives considered

- *Hard-coding one model now:* rejected, because the user has not decided.
