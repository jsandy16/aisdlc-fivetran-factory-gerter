# factory_core (Phase 2)

Depends only on the standard library and pydantic, so it can be vendored. Contents: error taxonomy and exit codes,
retry (full jitter, Retry-After, deadline), canonical JSON / sha256, CEL, yamlio, the `--json` result envelope, and
JSON logging with secret redaction. Most of it is adapted from `pf_runtime` / `pf` ([reuse](../../docs/architecture/reuse.md)).
