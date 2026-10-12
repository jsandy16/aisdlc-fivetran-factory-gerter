---
name: extending-pipeline-factory
description: Extends the pipeline factory itself with a new check kind, transform, source type, destination type or generator, keeping specs, runtime, catalog, scaffold and tests in step. Use when a requested rule, source or output cannot be expressed with `pf catalog` and the framework must grow.
---

# Extending the pipeline factory

A capability exists in exactly one model, one runtime implementation and one test. The catalog, JSON Schema,
scaffold comments and agent knowledge derive from the model automatically.

## New check or transform kind

1. Model: add a class to `src/pf/specs/rules.py` (`kind: Literal["<new>"]`, typed params with
   `Field(description=...)`) and add it to the right union (`PackageCheck`, `PackageTransform`, `EntityCheck`, `RowCheck`,
   `AttributeCheck`, `AttributeTransform` in `specs/rules.py`, or `LakeCheck` in `specs/quality.py`).
2. Runtime: implement it in `src/pf_runtime/` (`rules.check_passes`, `rules.apply_transforms`,
   `package.run_receive_checks`/`run_member_checks`, or `generate/quality.check_sql`). Messages must never
   contain data values.
3. Tests: a unit test that passes and one that fails in `tests/`, and a bad fixture in one example under
   `pipelines/` (rebuild it with its `tests/fixture_builders/<example>.py`).
4. Run `pytest -q` and `pf --json catalog` to see it listed.

## New source or destination type

Add a source model to `specs/source.py` (+ `Source` union, `FILE_SOURCES` if file-based), a session in
`src/pf_runtime/sources/` with the same interface (`list_files/download/read_text/close` or `columns/read/close`),
a scaffold template in `scaffold.py`, and in `generate/connector.py`: the module in `SOURCE_MODULES`, every new
third-party import in `IMPORT_TO_DIST`, and pins in `PINS` written lowercase with `_` (the Connector SDK compares
requirement lines literally and would rewrite others at deploy). `pf build` fails on any unpinned import.
Add a scenario to `dryrun/run.py` against a local stand-in of the source and run it with the Fivetran tester.

## Compatibility contract

Existing specs must keep validating: add optional fields with defaults only. A behaviour change is a new kind
(`<kind>_v2`), never an edit of an old one. Bump the framework version in `src/pf/__init__.py` and
`src/pf_runtime/__init__.py`; every pipeline's `pf build --check` will show the regenerated runtime, and each
pipeline adopts it in its own reviewed release.
