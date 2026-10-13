# ADR-0017: Byte-identical generation: canonical lock, toolchain image, no per-file stamps, .tf.json, deps lock separate

- **Status:** Accepted
- **Date:** 2026-10-13

## Context

The same spec must produce identical output on any machine. pf 0.1.0 stamps its version and spec hash into every generated file, so any upgrade or spec edit rewrites everything.

## Decision

- **Build hash:** canonical `spec.lock` plus the generator, template, op-contract, engine-table, pricing and toolchain-lock hashes go into `BUILD-MANIFEST.json`.
- **Generation environment:** generate and verify in a toolchain image pinned by digest.
- **No timestamps, ids, paths or per-file version stamps** in output.
- **Generator rules:** Jinja StrictUndefined, ordered iteration, LF line endings.
- **Terraform:** per-pipeline stacks are canonical `.tf.json`.
- **Dependencies:** `factory deps lock` is separate and committed; `generate` is offline.
- **LLM code is committed and keyed by contract hash**, and never re-prompted.
- **Proof:** `factory verify` diffs regenerated output in CI on Linux and macOS.

## Consequences

- Reviews show real changes only.
- Framework upgrades that change nothing produce zero diff.

## Alternatives considered

- *HCL generation with `terraform fmt`:* rejected, because it ties output to the formatter version.
