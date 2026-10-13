# ADR-0015: Account per team per environment (dev, test, ppt, prod); platform account per environment

- **Status:** Accepted
- **Date:** 2026-10-13
- **Amended:** decided: account_per_team, 4 environments (D7).

## Context

The user decided that each team has its own AWS account per environment: dev, test, ppt, prod (D7).

## Decision

- `platform-settings/<env>.json` declares `account_model: account_per_team`, the platform account, the team accounts, placement aliases, naming patterns for cross-account roles, shared stores and limits.
- Specs never contain account ids; `environments.yaml` names placement aliases only.
- The generators emit cross-account roles (launcher, trigger, deploy) and, when a destination lives in another account, bucket and KMS key policies.
- `shared_per_env` stays representable in the settings model, but is not used.

## Consequences

- The account boundary is the team isolation boundary.
- MWAA and the DAG publisher are the only shared components.

## Alternatives considered

- *Shared accounts per environment:* rejected by the user.
