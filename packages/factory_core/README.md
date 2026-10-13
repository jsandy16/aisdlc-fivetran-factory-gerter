# factory_core

Small, dependency-light utilities shared by every Pipeline Factory package (standard library and pydantic; YAML and
CEL are optional extras). Much of it is adapted from pf 0.1.0 ([reuse](../../docs/architecture/reuse.md)).

| Module | What |
|---|---|
| `errors` | error classes and exit codes: transient 75, data 65, config 78, security 77, internal 70 |
| `canonical` | canonical JSON bytes and sha256 (`doc_sha256`); floats and non-JSON types are refused |
| `envelope` | `Issue`, `Result` (`api_version: factory/v1`), `CommandError` (the `--json` contract) |
| `units` | sizes (`10GB`, `512MiB`) and durations (`1h30m`) as integers; retention periods (`10y`) |
| `yamlio` | round-trip YAML that keeps comments and order; aliases, tags, dates, duplicate keys and oversize files are refused with codes |
| `cel` | CEL with null-aware comparisons and fail-closed evaluation; `referenced_paths` for validation |
| `templates` | `{study_id}_{package_id}.zip` templates: a hand-written scanner, never `str.format` |
| `uri` | the strict destination URI parser (s3, sftp, https) used for portal-supplied destinations (ADR-0024) |
| `pathmatch` | allow-list path patterns: literals, `{var}` (must equal the run's value), `*`, trailing `**` |

Retry and JSON logging with redaction arrive in Phase 3 with their first consumer.
