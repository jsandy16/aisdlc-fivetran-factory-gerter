# Reuse inventory: pipeline-factory 0.1.0 → Pipeline Factory v2

`pf` and `pf_runtime` stay **frozen**. pf keeps passing its own suite and dry run (pytest: 270 passed / 14 skipped;
dry run 91/91). New packages *adapt* proven code from pf rather than importing pf internals.

Verdicts: **R** = reuse almost as-is, **A** = adapt (extract, parameterise), **P** = pattern only.

| pf 0.1.0 code | Verdict | v2 home | What changes |
|---|---|---|---|
| `pf_runtime/errors.py`: `TransientError`, `ConfigError`, … | A | `factory_core.errors` | Becomes the four-class taxonomy: transient / data / config / security, plus exit codes ([execution-model.md](execution-model.md)). |
| `pf_runtime/retry.py`: `retry`, `is_transient` (full jitter) | A | `factory_core.retry` | Adds HTTP status and `Retry-After`, an overall deadline, and httpx exception classes. |
| `pf_runtime/logx.py` | P | `factory_core.jsonlog` | JSON lines instead of key=value; correlation ids; a redaction filter for registered secret values. |
| `pf_runtime/package.py`: `hash_file`, `sniff`, `_safe_member`, `_unzip*` (zip-slip, bomb, symlink, duplicate, encrypted) | A | `factory_runtime.ops.package` | Extracted from `LocalPackage`/`PackageRejected`; a streaming variant added. |
| `pf_runtime/package.py`: `archive_original`, `put_evidence`, `_s3_errors` (write-once `IfNoneMatch="*"`, 412 = success) | A | `factory_runtime.ops.s3` | Default credential chain (Pod Identity / Lambda role) instead of static keys; checksum compare on 412 ("adopt the winner"). |
| `pf_runtime/sources/{sftp,ftp,tlsx}.py`: pinned host key, verified TLS, session interface | A | `factory_runtime.sources` | A `FileSource` protocol plus `open_stream()`; secrets via `SecretProvider`; a CA temp file that is deleted. |
| `pf_runtime/cel.py`: null-aware CEL evaluation | R | `factory_core.cel` | none |
| `pf/envelope.py`: `Result`, `Issue`, `PfError` | A | `factory_core.envelope` | `api_version: factory/v1`. |
| `pf/yamlio.py`: round-trip, aliases rejected | R | `factory_core.yamlio` | none |
| `pf/edit.py`: `apply_edit` with rollback | A | `factory_spec.patch` | Allowed files and validator are parameters; typed patch operations for agents. |
| `pf/lock.py`: `canonical`, `sha256` | R | `factory_core.canonical` | none |
| `pf/bundle.py`: loader pattern, `_loc`, `_cel_names`, `_python_ref_ok` | A | `factory_spec.load` | New file set; the Fivetran-specific `cross_validate` is not reused. |
| `pf/specs/common.py`: `StrictModel`, `Identifier`, `SemVer`, `<REQUIRED>` | A | `factory_spec.common` | `SecretRef` becomes `{provider, path, key, version}`. |
| `pf/catalog.py`: `describe_model` | R | `factory.catalog` | Catalog generated from op contracts. |
| `pf/generate/__init__.py`: `render_all`, `check`, manifest | A | `factory.verify` | No per-file version headers; generic ignore list. |
| `pf/generate/connector.py`: `check_imports` (AST allow-list incl. dynamic imports) | A | `factory.validate.imports` | Applied to custom rules and bundles. |
| `pf/generate/terraform.py`: `_bucket` (Object Lock, KMS, TLS-only) | P | `platform/modules/*` | Becomes hand-written HCL modules; per-pipeline stacks become `.tf.json`. |
| `pf/generate/codeowners.py`: managed block | A | `factory.generate.codeowners` | New file-owner map. |
| `pf/release.py`: `_zip` (deterministic), `_upload` (write-once, verified) | A | `factory.generate.bundle` | Generic exclusions. |
| `pf/llm/base.py`: `run_structured`, `strict_schema`, `fence`, `redact_secrets`, record/replay, tamper check | A | `factory.agents.llm` | Logical model id in the replay key; per-agent task names. |
| `pf/llm/agent_sdk.py`: isolated Agent SDK session | A | `factory.agents.runner.AgentRunner` | Skill, tools, schema, hooks and system prompt become parameters; isolation defaults kept. |
| `pf/rulecompiler.py`: acceptance digest, mutation testing, `_identity` | P | `factory.rules.acceptance` | The same pattern for custom-rule acceptance; mutmut for Python. |
| `pf/hooks.py`: fail-closed, tokenising pre-tool hook | A | `plugin/` for v2 | Configurable protected paths and person-only commands. |
| `pf/intake.py`: `extract_text`, merge with evidence | A | `factory.agents.intake` | Typed spec patches; never fills governance or security fields. |
| `pf/testing.py`: `LocalDirSession`, `NullLog`, break-and-verify | A | `testing/` | The good/bad fixture pattern is reused for op contract tests. |
| `dryrun/servers.py`, `mock_fivetran.py` | A | `testing/` | Adds HTTPS crawl, mock portal (TLS/mTLS) and a Vault dev server. |
| `pf_runtime/engine.py`, `rules.py`, `values.py` | P | — | Row-oriented and Fivetran-specific; stays in pf for the `fivetran_sdk` engine. |

## Known pf 0.1.0 issues (not fixed in Phase 1: pf is frozen)

| Issue | Where | Planned handling |
|---|---|---|
| A test hardcodes `/home/claude/pipeline-factory/.venv/bin/pf` | `tests/adversarial/test_cli.py:14` | Fix in Phase 2 (resolve from `sys.executable`). |
| `requires-python >=3.10` but `tomllib` needs 3.11 | `pyproject.toml` | Fix in Phase 2. |
| Local install docs omit `-c build/connector/requirements.txt`, so `pf deploy` preflight fails | `README.md` | Fix in Phase 2. |
| `src/pipeline_factory.egg-info/` is tracked in git | repo root | Remove and gitignore in Phase 2. |
| `FtpSession.read_text` reads whole files; `tlsx.ca_file` never deletes its temp file | `pf_runtime/sources` | Fixed in the v2 adapted copies; pf left as is. |
