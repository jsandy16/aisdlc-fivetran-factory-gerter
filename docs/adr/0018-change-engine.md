# ADR-0018: Incremental change from output diffs, explained by a semantic spec diff

- **Status:** Accepted
- **Date:** 2026-10-13

## Context

After generation, requirement changes must touch only the affected artefacts, show the infrastructure delta, protect in-flight runs and leave a changelog.

## Decision

- **Process:** a change request goes through the change-intake agent (spec patches), then validation, then in-memory regeneration.
- **ChangePlan** = semantic diff by stable ids (*why*) + `BUILD-MANIFEST` hash diff (*what files*) + `terraform plan -json` per affected stack (*what infrastructure*).
- **Only the affected pieces are re-done:** changed custom rules are re-reviewed and re-accepted, and affected tests are run.
- **Destructive changes** to stateful resources need approval. Modules use `prevent_destroy`, and renames generate `moved` blocks.
- **DAGs:** a breaking change to DAG shape creates `<pipeline>__v<major>`, and the old DAG is retired.
- **Changelog:** `changes/<n>.json` links the request, the plan and the approvers.

## Consequences

- Impact is exact rather than predicted.
- Regeneration is cheap.

## Alternatives considered

- *Predicting impact from spec diffs alone:* rejected, because it misses transitive effects.

## Phase 6 amendments

- Offline, *what infrastructure* is a resource-address diff of the generated `.tf.json` (create / update / destroy);
  `terraform plan -json` against real state joins in Phase 8.
- Breaking = task added or removed, `depends_on`, trigger type, `run_key`; majors 0 and 1 keep `<pipeline>` as the
  `dag_id`, major ≥ 2 gets `<pipeline>__v<major>`. Approvals are flagged (governance: security and destinations;
  platform: breaking changes and destroyed stateful resources), never granted by the CLI.
- `changes/<n>.json` holds the request, the plan and its sha256; approvers are filled from the PR.
