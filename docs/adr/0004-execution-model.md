# ADR-0004: Tasks are ordered rule lists run by one engine; a framework image plus a per-pipeline bundle verified by hash; run pinning; typed staged artefacts

- **Status:** Proposed (Phase 1 review)
- **Date:** 2026-10-13

## Context

Pipelines are custom, numerous and must be reproducible and safe to rerun. Building one container image per pipeline would mean hundreds of builds and scans. XCom and conf are visible in the Airflow UI.

## Decision

- **One engine invocation per task.** An orchestration task runs its rules in order in a single invocation.
- **One framework image per release**, used by every pod and Lambda and pinned by digest.
- **Per-pipeline code travels in a bundle.** The bundle is a deterministic zip of `spec.lock.json`, custom rules and a manifest, stored write-once and content-addressed. The workload verifies its sha256 (embedded in the generated DAG or Lambda configuration) before running it.
- **Run pinning.** The first task records `{bundle_sha, image_digest, logic_sha, binding_sha}` in run-state, and later tasks use that pin.
- **Typed artefacts** declare a durability (`stream`/`local`/`staged`/`final`). Only staged artefacts cross tasks, in a per-run staging prefix under the team KMS key. Only upload/copy ops write final targets.
- **Exit codes:** 0 ok, 75 transient, 65 data, 78 config, 77 security. Non-transient failures do not retry.

## Consequences

- One image to scan and sign per release.
- Deploys cannot mix versions inside a run.
- The dataflow is validated statically.
- Staging adds S3 traffic between tasks; the planner suggests fusing tasks where it matters.

## Alternatives considered

- *Per-pipeline images:* rejected, because of build and scan volume.
- *ConfigMaps for the lock:* rejected, because of the 1 MiB limit and mutability.
- *XCom for data:* rejected, because it is visible and size-limited.
