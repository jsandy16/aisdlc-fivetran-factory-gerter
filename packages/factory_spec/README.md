# factory_spec

Spec v1 for Pipeline Factory: six owner-scoped YAML files per pipeline, merged into one `PipelineSpec`. See the
[spec outline](../../docs/architecture/spec-outline.md), [ADR-0003](../../docs/adr/0003-spec-layout.md) and the
[Phase 2 design](../../docs/architecture/phase-2-design.md).

| Module | What |
|---|---|
| `sections/` | Pydantic v2 models (`extra=forbid`, frozen) for `pipeline`, `rules`, `integrations`, `security`, `environments`, `destinations` |
| `ops.py`, `catalog/` | params model per op; the op contract catalog and engine table as versioned JSON (`planned` ops are accepted by the schema and rejected by the validator) |
| `load.py` | loads the six files, reports `<REQUIRED>` placeholders once, translates pydantic errors into codes with selector locations (`rules[r_zip].implementation.params.level`) |
| `validate/`, `codes.py` | cross-file checks; every code has a plain-English hint and an invalid-corpus test |
| `derive.py` | the artifact graph: producers, consumers, kinds and durability (stream, local, staged, final) |
| `lock.py` | `spec.lock.json` with `logic_sha` (same in every environment), `binding_sha` per environment and `derived_sha` |
| `schema_export.py`, `schemas/` | deterministic JSON Schemas for editors (committed; CI checks drift) |
| `migrate/` | chained schema migrations over round-trip YAML (none yet for 1.0.0) |
| `patch.py` | `set` / `add` / `remove` edits that keep comments and roll back edits that add errors |
| `dynamic.py` | strict models generated from field specs (SQS messages, portal responses) |
| `ba_view.py` | the plain-English view for business analysts |

`pydantic` is pinned exactly: the exported schema bytes depend on its version (recorded in `schemas/SCHEMAS.json`).
`tools/gen_op_catalog.py` regenerates `catalog/op_catalog.v1.json`; the committed JSON is what runs.
