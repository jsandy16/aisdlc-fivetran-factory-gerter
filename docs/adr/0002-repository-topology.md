# ADR-0002: Framework repo, one pipelines repo per team, platform repo

- **Status:** Accepted
- **Date:** 2026-10-13

## Context

Specs contain confidential study ids and server paths. Many teams must work independently. Shared infrastructure (MWAA, EKS, VPC, Vault auth) must not be changed by any single team.

## Decision

- **Framework repo** (this repo): the versioned tool.
- **Team pipelines repo** per team or isolation domain: specs, locks, committed custom rules, synthetic fixtures, approvals, change records, generated output and workflows. GitHub OIDC `sub` → team deploy role.
- **Platform repo:** shared Terraform modules (semver tags), live stacks, MWAA requirements/plugins, the central DAG publisher, and `platform-settings/<stage>.json`.
- Until org repos exist, examples of the team and platform repos live under `examples/team-pipelines/` and `platform/` here.

## Consequences

- GitHub read access becomes the confidentiality boundary for specs.
- Cross-repo versioning is needed: teams pin the framework and module versions in `factory.toml`.

## Alternatives considered

- *One org-wide pipelines repo:* rejected, because there is no read isolation between teams.
- *One repo per pipeline:* rejected, because hundreds of repos are too much overhead.
