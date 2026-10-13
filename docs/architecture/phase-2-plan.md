# Phase 2 plan: spec models, schema, starters, worked example

Starts after the Phase 1 review and the answers (or accepted fallbacks) for Q1, Q2 and Q6.

## Deliverables

1. **Workspace setup.**
   - Turn the repo into a uv workspace with `packages/factory_core` and `packages/factory_spec`.
   - pf 0.1.0 stays in `src/` unchanged, except the small hygiene fixes listed in [reuse.md](reuse.md): the
     hardcoded test path, `requires-python`, README install constraints, and untracking `egg-info`.
2. **`factory_core`:** `errors` (taxonomy + exit codes), `retry`, `canonical`, `cel`, `yamlio`, `envelope`, adapted
   from pf with tests.
3. **`factory_spec`:**
   - Pydantic v2 models for every section in [spec-outline.md](spec-outline.md).
   - Discriminated unions per rule category and per op (op params models for the v1 catalog, contracts as data).
   - The secret-reference model.
   - Portal models, including generating a deterministic response model from `response_schema`.
   - Environment bindings, and the logic/binding hashes.
4. **Validators.**
   - Schema checks.
   - Cross-field rules (the table in spec-outline §4).
   - Reference resolution and the dataflow graph.
   - The engine feasibility check, which needs the engine table in a first version.
   - GxP tier rules.
5. **Tooling.**
   - JSON Schema export: deterministic, with `x-owner-role`, `x-ba-label` and examples.
   - Migration framework with a dummy `1.0.0 → 1.1.0` migration and golden tests.
6. **CLI (Typer):** `factory init`, `validate`, `schema`, `show --view ba`, `migrate`, each with a `--json`
   envelope.
7. **Starter templates** (spec folders): `portal_zip_encrypt_s3`, `s3_to_s3_event`, `table_to_s3_fivetran`,
   `rest_to_s3`.
8. **Worked example:** the 4-task requirement as a complete spec under
   `examples/team-pipelines/pipelines/study_pkg_delivery/`, using the Q1/Q2 answers or the fallbacks.

## Exit criteria

- The worked example and all 4 starters validate with zero open questions.
- The invalid-spec corpus (at least one case per cross-field rule) produces the expected error codes, with
  plain-English fix hints.
- YAML ↔ model round trip preserves comments and order.
- The JSON Schema export is byte-stable, and its hash is recorded.
- `pytest`, `ruff` and `mypy --strict` are clean for the new packages. pf's suite (270/14) and the dry run (91/91)
  stay green.
- A BA walkthrough of `factory show --view ba` for the worked example is reviewed.

## Not in Phase 2

Runtime ops, agents, generators, Terraform. Those are Phases 3–5.
