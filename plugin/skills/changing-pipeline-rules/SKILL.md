---
name: changing-pipeline-rules
description: Adds, changes or removes structural validations, data-quality checks and transformations at package, entity, row or attribute level in a pipeline-factory pipeline, including plain-English rules compiled to logic at build time, and handles the version bump for pipelines already deployed. Use when someone asks for a new validation, check, data-quality rule, transformation, naming or checksum rule, or a change to an existing one.
---

# Changing pipeline rules

## Where a rule goes

| Need | Scope | File / command |
|---|---|---|
| File name, size, checksum, package type, members, folder structure | package | `pf add-check <p> --package '<yaml>'` |
| Unzip, gunzip, member renames | package transform | `pf add <p> source.yaml source.package.transforms '<yaml>'` |
| Row count, uniqueness, control-file count | entity | `pf add-check <p> --entity <e> '<yaml>'` |
| Rule across columns | row | `pf add-check <p> --entity <e> --row '<yaml>'` |
| One column: not_null, regex, range, allowed values, length | attribute | `pf add-check <p> --entity <e> --attribute <a> '<yaml>'` |
| Trim, map codes, pseudonymise, derive | attribute transform | `pf add <p> sttm.yaml 'entities[<e>].attributes[<a>].transforms' '<yaml>'` |
| Whole-table checks after load (uniqueness, freshness, referential) | lake | `pf add <p> quality.yaml checks '<yaml>'` |

Run `pf --json catalog` for the exact kinds and parameters. Each check needs a unique `id`, a `description`,
and an `on_fail` the owner chose (`quarantine_row`, `reject_package`, `reject_entity`, `fail_sync`, `warn`).

## Custom logic

1. Prefer a built-in kind. Next, `kind: expression` (CEL; `row.<col>`, `value`, `package.<fact>`).
2. Plain English: `kind: english` with `text` and at least 3 `examples` the owner labelled by hand
   (`{input: {...}, expected: pass|fail}`). Then:
   ```bash
   pf --json rules compile <p>
   ```
   Show the owner each rule's `expression`, `back_translation`, cases and `ambiguities` verbatim. If there are
   ambiguities, the owner rewords the text and you compile again. Only the owner accepts — you never run
   `pf rules accept`.
3. `kind: python` only for logic CEL cannot express; a data engineer writes `rules/<module>.py` and it is code-reviewed.

## Changing a deployed pipeline

New rules start with `mode: shadow` (evaluated and counted in `pf_rule_results`, no action). After the owner
has reviewed the counts, a second change sets `mode: enforce`. Bump `version` in pipeline.yaml: additions and
rule changes are minor; removing entities/attributes or changing types or keys is major (`pf release --check`
tells you what is required). Every new enforced check needs a failing fixture under `fixtures/bad/<case>/`.
