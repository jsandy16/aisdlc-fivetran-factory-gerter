# ADR-0025: Infrastructure ownership per environment, and AWS resources by configuration

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

The framework's job is the end-to-end pipeline. The AWS resources it needs may be created by the framework, created in
advance by an infra team, or handed over later. Adding a service (a topic, an alarm, a dashboard) must be a change of
configuration or spec, not of framework code.

## Decision

- **Ownership per environment** (`environments.yaml` → `infrastructure`). Each group is `create` (default) or
  `existing`, with a default for the whole environment. The groups are `roles`, `queues`, `logs`, `compute`, `alerts`
  and `resources`.
  - The generator always computes the full stack. An ownership pass then removes `existing` resources, turns
    references to them into `data` lookups (so a missing one stops `terraform plan`), and writes the handover
    contract `build/<pipeline>/infra/<env>.json|md`. The contract holds the exact settings and IAM policies the
    framework would have used, with references spelled out as names and ARNs.
- **Ownership changes later.** `existing` emits `removed { lifecycle { destroy = false } }`, so the stack releases what
  it managed before without deleting it. `adopt: true` emits `import` blocks; AWS-generated ids come from `import_ids`.
  `factory change plan` reports these as *release* and *adopt*, never as destroy or create.
- **Resource catalog** (`factory_spec/catalog/resources.v1.yaml`). Each resource type is data:
  - Terraform resource templates with a small token language;
  - a parameter schema (validated by `factory validate`);
  - secure defaults (KMS, versioning, no public access);
  - a lookup field and an import id.

  Instances are declared once in `environments.yaml` → `resources`. They are bindings, so `logic_sha` is unchanged.
  One generic renderer feeds them into the same ownership pass.
- **Alert routing is configuration.** `observability.alerts` plus `alert_channels` generate `TaskFailed` alarms per
  failure class (a runtime metric) and a dead-letter alarm. Topics named by `sns` channels are looked up; `email` and
  `slack` channels get a topic of their own.
- **One generic op, `notify.publish`.** It sends ids and a subject (never data) to a declared topic, with
  least-privilege `sns:Publish`.

## Consequences

- A team can deploy into a landing zone it does not control. The infra team receives a precise, generated handover.
- New infrastructure services are a reviewed catalog entry plus a spec change, as long as they need no new runtime
  behaviour. A step that reads or writes a new kind of system still needs an op or a custom rule (ADR-0020).
- `existing` resources are checked by `terraform plan` (lookups) and, optionally, by `factory infra check` (existence,
  plus the IAM policy simulator for the roles).

## Alternatives considered

- *A flag per resource in the generator code:* rejected. Every new resource type would need code, and the handover
  would be written by hand.
- *Terraform modules chosen by the infra team:* kept as possible future work. The contract describes what any module
  must provide.
