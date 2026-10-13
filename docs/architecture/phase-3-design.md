# Phase 3 design: runtime, secret provider, portal client, plugin interface

Approved by the user on 2026-10-13 (answers: built-in httpx Vault client; keep the full scope; HTTPS verification
"best available, flagged"; SFTP folders created below the approved root). This document is the plan of record for
Phase 3; deviations found while building are recorded in the "As built" section at the end.

## 1. Context

Phases 1–2 are delivered and pushed on `feature/pipeline-factory`. Phase 2 head is `44c4db3`. The user approved
Phase 3 with these defaults:
- Q9: x and y are both required; a retry completes the missing one.
- Q10: Fivetran is driven through its REST API.
- Q11: v1 destinations are s3, sftp and https.
- Q12: the portal response contract is the spec's field specs.
- Vault login uses AWS IAM auth.
- HTTPS uploads send `If-None-Match: *`.

Phase 3 makes the specs **executable**. A task of a validated pipeline runs as
`python -m factory_runtime.run …` (pod) or `lambda_handler.handle` (Lambda) and does the following:
- moves real bytes;
- resolves secrets without leaking them;
- calls the portal;
- delivers only to allow-listed destinations;
- exits with the 0/65/75/77/78 contract.

**Constraints:**
- Everything runs locally against stand-ins; no AWS before Phase 8 (D6).
- Synthetic data only.
- Secrets are never logged or persisted.
- pf 0.1.0 stays frozen: code is adapted from it, never imported.

**Stopping point:** at the end of Phase 3, stop and present: what was built, the decisions, open questions, and the
Phase 4 plan.

### Decisions recorded for this phase

| # | Decision |
|---|---|
| P3-1 | **Vault:** a built-in httpx client replaces hvac. It makes five calls: AWS IAM login (SigV4 via botocore against regional STS), KV v2 read by version, renew-self, lookup-self, health. This keeps one TLS policy, adds no `requests` dependency and avoids the token leak on redirect. |
| P3-2 | **Scope "keep everything":** all 19 available ops, including `rest.fetch`, `s3.copy`, SQS `in_task` mode and `fivetran.sync` (REST trigger and wait), plus the deterministic `auto` engine planner with placeholder pricing and `factory plan`. |
| P3-3 | **Q13, HTTPS verify after write:** use the best evidence available. In order: the `Repr-Digest` returned by the server; a GET read-back hash if the endpoint allows GET; size only. The receipt and audit event record `verification: digest \| read_back \| size_only`. |
| P3-4 | **Q14, SFTP folders:** create missing folders only *below* the allow-list entry's literal root, never above it. |
| P3-5 | **Q15, quarantine (default taken):** `on_failure: quarantine` fails the run as `data` and writes a `quarantined` run-state record. Moving source objects is deferred. |
| P3-6 | **Q16, Vault namespace (default taken):** the team namespace from the binding is used for both the KV read and the auth mount. It is configurable in platform settings. |
| P3-7 | **Custom rules get no secrets in v1.** `CustomImpl` has no secrets field. The docs are amended. |
| P3-8 | **Engine decisions are never written to the lock.** `factory plan` prints them; Phase 5 writes `build/plan.json`. |

## 2. Packages and dependencies

**`packages/factory_runtime`** (shipped):
- Dependencies, all exact pins: `factory-spec`, `boto3==1.43.111`, `botocore==1.43.111`, `httpx==0.28.1`, `paramiko==5.0.0`.
- No hvac, no requests.

**`packages/factory_testing`** (dev only, never a runtime dependency):
- Stand-ins and harness. Dependencies: `factory-runtime`, `moto[server]==5.2.4`, `asyncssh==2.24.1`, `cryptography`.
- `testing/README.md` at the repo root points to it, as `starters/` does.

**`packages/pyproject.toml`:**
- Add both packages as members and to `[tool.uv.sources]`.
- Extend `testpaths` and mypy `files`.
- Add mypy `ignore_missing_imports` for boto3, botocore, paramiko, asyncssh and moto.
- Add pytest markers `spike` and `slow`; addopts `-m "not spike"`.
- Add `factory-testing` to the dev group.
- Regenerate `uv.lock`.

**Test file prefixes:**
- `test_core_*` (existing), plus new `test_rt_*` (runtime), `test_stand_*` (stand-ins) and `test_plan_*` (planner, in the `factory` package).
- Helper modules get unique names, e.g. `rt_helpers.py`.

## 3. Design (what each area does)

**factory_core additions:**
- `retry.py`:
  - full jitter with injectable random, sleep and clock;
  - `Retry-After` as seconds or HTTP-date;
  - an overall deadline;
  - classification by exception class name, adapted from pf `retry.py`. SSL verification failures (walking `__cause__`) are **security**; 401/403 are config.
- `jsonlog.py`:
  - JSON lines with fields bound through contextvars.
  - A redaction registry: a value is registered when a `SecretValue` is **constructed**; each line longer than 8 characters of a multi-line value is also masked (from pf `_mask`). Derived forms are registered too: base64 Basic auth, url-encoded, JSON-escaped, and OAuth access tokens.
  - A pattern fallback from pf `SECRET_PATTERNS`.
  - Third-party loggers (`botocore`, `urllib3`, `httpx`, `httpcore`, `paramiko`) are clamped to WARNING.
  - An EMF metric helper.
- `runkey.py`: `run_dir(run_key) = sanitise(run_key)[:200] + "-" + sha12`, using `[A-Za-z0-9_.-]` so it fits Airflow's run-id pattern. Shared by the dispatcher, the runtime and Airflow (Phase 5).
- `globmatch.py`: one portable glob dialect (`*`, `**`, `?`, `[...]`), shared with the validator.
- `cel.evaluate_value()`: for naming expressions.

**factory_spec additions** (pure logic, so the `factory` CLI never imports boto3, paramiko or httpx):
- `estimate.py`: `estimate_lambda_seconds` moved here from `validate/checks.py`.
- `resolve.py`: pure destination matching.
  - Parse with `factory_core.uri`.
  - Type, host or bucket, and port must be equal.
  - The slot must be in the entry's `slots`, and the type in the slot's `allowed_types`.
  - The path must match `factory_core.pathmatch` with the run's variables.
  - No match → `SecurityError destination_not_allowed`; more than one match → config error.
- `facets.py`: the CEL view of each artifact kind (e.g. listing → `{count, total_bytes}`).
- `artifact_models.py`: canonical JSON models for message, snapshot, destination_set, listing, manifest v1, checksum and receipt.
- `bundle.py`: a deterministic **ZIP_STORED** bundle containing `spec/*.yaml`, `spec.lock.json`, `rules/` and `BUNDLE-MANIFEST.json` (factory version, pydantic version, zlib version, sha per file).
- `imports.py`: the plugin import allow-list checker. New validator code `custom_rule_import_forbidden`.
- `catalog/pricing.v1.json`: Lambda GB-s and request prices; EKS vCPU-h and GB-h; spot discount. Values for us-east-1 and eu-west-1 from the AWS Price List API, marked as placeholders until Phase 6/8.
- Spec amendments (re-lock; the golden lock and schemas are regenerated):
  - `SftpEntry.host_key_algorithm` (`ssh-ed25519 | ecdsa-sha2-nistp256 | rsa-sha2-256`), so the negotiated key type matches the pin.
  - `VaultSettings.role` and `iam_server_id` in platform settings.
  - A POST https entry with `write_once: if_none_match` is a new config error.
  - New validator codes: `glob_invalid`, `custom_rule_import_forbidden`, and the HTTPS POST/write-once check above.
  - The worked example's ppt/prod HTTPS `path_pattern` becomes `/api/v2/studies/{study_id}/packages`. **A destination URI is always a folder; the target is `folder/<object_name>`.**
  - dev/test SFTP stub fingerprints become those of **seeded** ed25519 keys (derived at test time; no private keys committed).

**factory_runtime** (`packages/factory_runtime/src/factory_runtime/`):

*Entry points:*
- `run.py`: `main(argv, *, overrides=None)` with args `--bundle s3://… --sha256 H --task T --run-key K [--attempt N --run-id R]`.
  - The production entry (`python -m factory_runtime.run`) and the Lambda handlers always pass `overrides=None`.
  - `Overrides(aws_endpoint_url, extra_ca_pem)` exists for in-process tests only.
- `lambda_handler.handle(event, ctx)` returns `{exit_code, error_class, code}`.
  - On a warm start it resets contextvars, cleans the workdir and caches bundles by sha.
  - It keeps an internal deadline from the task timeout and aborts multipart uploads on timeout or SIGTERM.
- `config.py`: `RuntimeConfig` from `FACTORY_*` environment variables. These are never secrets:
  - env, team, pipeline, region;
  - staging, run-state, bundle and audit stores and their KMS keys;
  - the placement-to-account map;
  - Vault address, role, mount and server id;
  - image digest.
- `envcheck.py`: in ppt and prod, refuse to start (77) if any of these are set: `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `AWS_CA_BUNDLE`, `AWS_ENDPOINT_URL*`, `*_PROXY`.

*Bundle, pin and state:*
- `bundle.py`:
  - Fetch the bundle and verify its sha. A mismatch is 77.
  - Extract safely, using pf unzip-safety rules.
  - Load it with `load_spec`, then require `lock_text(spec) == bundled lock`. A mismatch is 78 `bundle_framework_mismatch`.
- `pin.py`:
  - The first task writes the pin `{bundle_sha, image_digest, logic_sha, binding_sha, pinned_at}` with a conditional write.
  - A different `--sha256` loads the pinned bundle instead.
  - A different image digest is 78.
  - `run_date` and `now()` come from `pinned_at`.
- `state/`:
  - A `RunState` protocol with a memory fake and a DynamoDB single-table implementation.
  - Keys: `pk = <pipeline>#<run_dir>`; `sk` is `PIN`, `TASK#t`, `RULE#t#r`, `SNAP#…`, `QUAR`, `DISP#…`.
  - Sequence numbers live under `pk = <pipeline>#SEQ#<name>`: a conditional put of run_key → seq, otherwise an `ADD` on the counter. Retries reuse the number.
  - A TTL attribute equal to the replay window plus a margin.

*Staging and artifacts:*
- `staging.py`:
  - Staged objects live at `s3://<staging>/<pipeline>/<run_dir>/<artifact>`; `rev-<n>/` is reserved for forced reruns later.
  - Writes are write-once. On 412, compare: equal → done; snapshots → adopt the stored one; otherwise → 65.
  - For large objects, write the hash record `<artifact>.meta.json` first, then Complete with `IfNoneMatch`. A 412 on Complete is resolved from the meta, and the multipart upload is always aborted.
- `artifacts.py`: typed handles per kind. `file_set` is an in-process iterator of `FileStream(rel_path, size, stream)`. `package` is the staged object plus member metadata (per-member sha256 and size; package md5, sha256 and size).

*Executor and audit:*
- `executor.py`, `context.py` (`RuleContext`):
  - Runs the task's rules in order.
  - CEL preconditions run before a rule.
  - Checkpoints apply only to rules whose outputs are all staged or final. A completed task is a no-op.
  - **Error precedence:** security is never downgraded; transient is never reclassified; `on_failure.error_class` applies only to check failures.
  - `warn` is allowed only on rules without outputs; `quarantine` follows P3-5.
- `codes.py`: a registry of runtime error codes, with a meta-test.
- `audit.py`: per task attempt, write-once JSONL in the audit store with the event types from `security.yaml`. The manifest is also copied to the audit store.
- Logs carry `run_key` as sha12 only. Full server paths are hashed at classification restricted or higher.

*Secrets and TLS:*
- `secrets/`:
  - `SecretValue`: masked `repr`, `str` and `format`; pickle, copy, deepcopy and JSON raise; `.reveal()` works only inside a `with` block.
  - `provider.py` (protocol `get(ref, *, purpose, run_id, rule_id)`).
  - `aws_sm.py` (boto3 GetSecretValue by VersionId or VersionStage; JSON key extraction).
  - `vault.py` (P3-1).
  - `cache.py`: TTL per kind; an auth failure invalidates the entry and refetches once.
  - `audited.py`: one event per `get`, never the value.
  - `router.py`: chooses the provider; paths templated with `{env}`, `{team}`, `{pipeline_id}` plus the binding prefix, mount and namespace.
- `tls.py`: one client factory for every HTTP call.
  - `trust_env=False`, an explicit SSLContext (certifi or a pinned CA), TLS 1.2 or later.
  - `follow_redirects=False`; a 3xx is a config error.
  - A response size cap.
  - mTLS key files are 0600, created with O_EXCL, and deleted right after `load_cert_chain`.

*Portal:*
- `portal/` (`auth`, `reliability`, `paging`, `client`, `mapping`):
  - **Auth:** OAuth2 client credentials with a cached token and one refresh on 401; api_key; mTLS.
  - **Reliability:** retry with backoff, a per-process circuit breaker, a token-bucket rate limit.
  - **Pagination:** none, page, cursor, link_header, with a page cap of 1000 (→ 78).
  - **Validation:** strict `dynamic.record_model(...).model_validate_json`. Errors use `include_input=False`.
  - **Mapping:** JMESPath. An `expect` mismatch is 77; a required slot missing from the answer is 78.
  - **Snapshot:** the canonical mapped entries only, written once and adopted on a 412. The raw response sha256 goes only into the audit event.

*Ops:*
- `ops/` registry, one module per family. A test checks that the implemented ops equal the catalog's available ops.
  - `sqs.read`: dispatch mode reads the custody snapshot; `in_task` mode receives, snapshots, dedupes and deletes.
  - `portal.lookup` and `destination.resolve`: resolve uses factory_spec `resolve.py` and adds an audit event.
  - `s3.list`:
    - The message prefix must end with `/` and start with a bound `prefix_root`.
    - Keys with `..`, `//`, a backslash or NUL are rejected; folder markers are skipped.
    - Include/exclude rules use `globmatch`.
    - Limits are enforced, and the listing snapshot is adopted on a 412.
  - `s3.download`: streams in key order with `GetObject(IfMatch=<listed ETag>)`; a changed object is 65. Read-ahead is bounded by bytes.
  - `package.zip` (`package/zipstream.py`):
    - stdlib zipfile writing to an **unseekable** sink, so every entry gets a data descriptor;
    - pinned `date_time` 1980-01-01, `create_system=3`, `external_attr=0o100644<<16`, compress type and level;
    - entries sorted by relative path;
    - `zip64: auto` sets `file_size` from the listing; `always` forces zip64;
    - `store_if_compressed` decides by extension plus magic bytes;
    - md5 (`usedforsecurity=False`) and sha256 of the zip are computed by a tee, and per-member sha256 while reading;
    - output goes to `transports/s3.MultipartWriter`: parts of `max(64 MiB, ceil(max_bytes/10000))`, bounded concurrency, Complete with `IfNoneMatch`, abort on any error.
  - `manifest.build`, and `checksum.compute` (streaming re-read of the stored object; mismatch → 65).
  - `validate.*`, expression rules and `naming.render` (path-safe render plus `{seq}` from run-state).
  - `destination.deliver`:
    - The target is the matched folder plus `object_name`, re-checked to stay under the folder.
    - Order: package, verify, manifest, then sidecars `<name>.md5` and `<name>.sha256` (GNU format) last.
    - **S3:** multipart UploadPartCopy from staging, Complete with `IfNoneMatch` and SSE-KMS using the entry's key ARN (`arn:…:<placement account>:alias/…`). Verify by full read-back hash.
    - **SFTP:** paramiko with `allow_agent=False` and `look_for_keys=False`, `disabled_algorithms` limited to the pinned type, and a policy that compares `key.fingerprint` with `hmac.compare_digest` (mismatch → 77).
      - Upload to a temp name `.<name>.<run12>.<uuid8>.part` opened `"xb"`, then a standard `rename` (never `posix_rename`).
      - If the rename fails, `stat` the target and compare its sha256: equal → ok, different → 65.
      - Create folders below the literal root only (P3-4). Verify by size plus read-back hash.
    - **HTTPS:**
      - Send a HEAD first: if the target exists, compare size and digest instead of uploading.
      - Otherwise stream a PUT with `Content-Length`, `If-None-Match: *` and `Repr-Digest`. On 412, HEAD and compare.
      - `write_once: none` means check then PUT. Verify as in P3-3.
  - `s3.put`, `sftp.put`, `https.put` and `s3.copy` (multipart copy), all to static slots.
  - `rest.fetch`: shared HTTP client and API auth plus pagination; each page becomes a `file_set` member.
  - `fivetran.sync`: REST `POST /v1/connections/{id}/sync`, then poll `GET /v1/connections/{id}` until `succeeded_at` or `failed_at` advances, or the timeout.
    - The Fivetran API key is a platform secret reference in `RuntimeConfig`, resolved through the SecretProvider.
    - The Airflow wrapper comes in Phase 5.

*Dispatcher and plugins:*
- `dispatch.py`, the dispatcher Lambda:
  1. Unwrap the envelope (raw, sns or eventbridge).
  2. Validate the message with `record_model`.
  3. Write a body-only custody snapshot, write-once.
  4. Write a receipt per message.
  5. Move through the states received → snapshotted → triggered, calling MWAA unless the state is already `triggered`; a 409 counts as triggered.
  - The MWAA call sits behind a `Trigger` protocol: a boto3 `mwaa.invoke_rest_api` implementation (tested with botocore `Stubber`) and a recording fake.
  - `conf` carries ids, URIs and hashes only.
  - Returns partial batch responses; a poison message is written to a quarantine record.
- `plugin_api.py` and `plugins.py`:
  - `CustomRule[Params, Inputs, Outputs]` with typed refs.
  - The loader checks `code_sha256` (77) and re-runs the allow-list check.
  - Allowed imports: `typing`, `dataclasses`, `datetime`, `decimal`, `re`, `json`, `math`, `hashlib`, `itertools`, `collections`, `enum`, `pydantic`, `factory_runtime.plugin_api`.
  - Banned names: `eval`, `exec`, `compile`, `__import__`, `getattr`, `setattr`, `delattr`, `globals`, `locals`, `vars`, `open`, `breakpoint`.
  - Any `__` in an attribute or string constant is banned.
  - Context calls are recorded in tests and must be a subset of the declared side effects.
  - ADR-0020 says plainly: this is a lint, not a sandbox.

**factory (build time):**
- `factory/plan/engine.py`: the deterministic planner from engine-selection.md §2. It uses `factory_spec.estimate` and `pricing.v1.json`.
- `factory plan <pipeline> [--json]` prints `engine_decisions`: inputs, candidates, filters that fired, estimates, costs and the choice.
- Expected for the worked example: t1 → lambda, t2 → lambda (explicit, feasible), t3 → eks_pod.
- `factory bundle <pipeline>` writes the deterministic bundle and prints its sha256.
- `factory test <pipeline>` runs the owner `test_cases` for expression, naming and `destination.resolve` rules, using factory_spec pure logic.

**factory_testing** (`packages/factory_testing/src/factory_testing/`):
- `certs.py`: a test CA built with cryptography.
- `netmap.py`: patches `socket.getaddrinfo` in tests only, so hostnames like `sftp-stub.dev.acme.example` resolve to 127.0.0.1 ports while SNI and certificate checks still see the real name. The runtime has no host-override hook.
- `moto_server.py`: threaded and subprocess variants.
- `fake_vault.py`: TLS; IAM login checks the server-id header and role; namespace; KV versions; sealed, 403 and deleted modes.
- `mock_portal.py`: built from the endpoint field specs; TLS with optional mTLS; synthetic answers; fault modes for 429 + Retry-After, a 503 burst, schema drift, attack URIs, a pagination loop, token expiry, mTLS rejection, timeouts and an oversize body.
- `mock_api.py` (for `rest.fetch`).
- `mock_fivetran.py` (sync and poll).
- `sftp_server.py`: asyncssh with a chroot and seeded keys, so rename onto an existing name fails.
- `https_sink.py`: TLS; bearer or mTLS; honours If-None-Match; switchable `Repr-Digest`; records requests.
- `synthetic.py`: deterministic package trees and large generated streams.
- `harness.py`: copies the example repo, rewrites dev bindings to the stand-in ports, builds and uploads the bundle, and runs the dispatcher and tasks in-process.

## 4. Milestones (commit and push after each; CI green on 3.11/3.12 throughout)

| # | Milestone | Main files | Tests (what is proven) |
|---|---|---|---|
| M0 | Workspace and spec amendments; write `docs/architecture/phase-3-design.md` from this plan | `packages/pyproject.toml`, `uv.lock`, the two new `pyproject.toml` files, `factory_spec/sections/destinations.py`, `platform.py`, validator codes, worked example, platform settings, schemas and lock | Phase 2 suite still green; new invalid-corpus cases; `schema --check` and `lock --check` |
| M1 | factory_core: retry, jsonlog, runkey, globmatch, `evaluate_value` | `factory_core/src/factory_core/*.py` | `test_core_retry` (fake clock and RNG; Retry-After; deadline; classification table), `test_core_jsonlog` (whole, multi-line and derived redaction; threads; logger clamping), `test_core_runkey` (hypothesis tests against the Airflow pattern), `test_core_glob` |
| M2 | factory_spec pure logic: estimate move, resolve, facets, artifact models, bundle, imports, pricing | `factory_spec/src/factory_spec/{estimate,resolve,facets,artifact_models,bundle,imports}.py` | `test_spec_resolve` (the example's `test_cases` plus 40 or more attack URIs), `test_spec_bundle` (PYTHONHASHSEED subprocesses; golden bytes), `test_spec_imports` (bypass attempts) |
| M3 | Runtime core without network: config, envcheck, codes, bundle, pin, state, staging, artifacts, executor, context, run, lambda_handler; ops `naming`, `validate`, expression | `factory_runtime/...` | `test_rt_bundle`, `test_rt_state` (moto; 8-thread `{seq}` race), `test_rt_staging`, `test_rt_executor` (precedence matrix, warn and quarantine, rerun does nothing, pin mismatch), `test_rt_entry` (every exit code; handler payload; environment-read recorder; ppt/prod hygiene) |
| M4 | Secrets, TLS and audit, plus stand-ins certs, fake_vault, netmap, moto_server | `secrets/*`, `tls.py`, `audit.py`, `factory_testing/*` | `test_rt_secret_value`, `test_rt_secrets_sm`, `test_rt_secrets_vault` (sealed → 75; 403 → log in again once → 78; renewal), `test_rt_secrets_cache` (rotation mid-run, R8), `test_rt_audit` (one event per `get`) |
| M5 | HTTP client, portal, `portal.lookup`, `destination.resolve`, `rest.fetch`; mock_portal, mock_api | `portal/*`, `ops/portal.py`, `ops/resolve.py`, `ops/rest.py` | `test_rt_portal_unit` (MockTransport: every fault, redirects refused), `test_rt_portal_tls` (untrusted certificate → 77; mTLS rejected → 78), `test_rt_resolve_op` (attacks → 77; snapshot adopted), `test_rt_rest_fetch` |
| M6 | S3 extract and streaming package: transports/s3, `s3.list`, `s3.download`, zipstream, `package.zip`, `manifest.build`, `checksum.compute` | `transports/s3.py`, `package/zipstream.py`, `ops/s3_*.py`, `ops/package.py`, `ops/manifest.py`, `ops/checksum.py` | `test_rt_zipstream` (determinism; zip64 auto and always; store magic; 3.11–3.13 golden bytes for store), `test_rt_s3_ops` (tampered zip → 65; listing mismatch → 65; source changed → 65; abort on 412). Opt-in `test_spike_s6` (about 2 GB, moto in a subprocess, RSS bound, throughput recorded) |
| M7 | Delivery: transports/sftp and https, `destination.deliver`, static puts, `s3.copy`; stand-ins sftp_server, https_sink | `transports/{sftp,https}.py`, `ops/deliver.py`, `ops/static_put.py`, `ops/s3_copy.py` | `test_rt_sftp` (host key → 77; existing equal → ok; different → 65; folders only below the root), `test_rt_https` (digest, read-back and size-only paths; 412 compare), `test_rt_deliver` (x ok, y transient failure, retry completes y), `test_rt_s3_copy` |
| M8 | Dispatcher and `sqs.read` in dispatch and `in_task` modes | `dispatch.py`, `ops/sqs.py` | `test_rt_dispatch` (Stubber for 200/409/5xx; 10-thread duplicate race, R3; conflicting body → quarantine; envelopes; partial batch), `test_rt_sqs_in_task` |
| M9 | `fivetran.sync` and mock_fivetran | `ops/fivetran.py` | `test_rt_fivetran` (success, failure → 65, timeout → 75, auth → 78) |
| M10 | Plugin interface | `plugin_api.py`, `plugins.py` | `test_rt_plugin` (sha mismatch → 77; forbidden import → 77 at load; side-effect recording) |
| M11 | Planner and CLI: `factory plan`, `factory bundle`, `factory test` | `factory/plan/engine.py`, `factory/cli.py` | `test_plan_engine` (worked example → lambda/lambda/eks_pod; policy filters; ties; explicit infeasible → error), CLI envelope tests |
| M12 | Acceptance (below), docs and ADRs, as-built section | `factory_testing/harness.py`, `test_rt_e2e_*` | See §6 |

## 5. Doc and ADR amendments

- **ADR-0004 and execution-model §1–§6:**
  - bundle contents; argv and the Lambda contract;
  - pin rules; `run_dir` and the staging layout; run-state key design;
  - error precedence, warn and quarantine.
- **ADR-0008, secret-provider and tech-stack:**
  - the httpx Vault client, with hvac removed;
  - Transit and AppRole deferred;
  - redaction at construction, including derived forms;
  - S7 limits (fake Vault; a real dev server only if a binary is available);
  - P3-6.
- **ADR-0009 and portal-integration:** module location; snapshot path and content; adopt on 412; redirects refused; `trust_env` off; key-file lifetime; `expect` mismatch → 77; missing slot → 78.
- **ADR-0024:** folder semantics; the HTTPS example pattern; `host_key_algorithm`; P3-4.
- **ADR-0013:** hash record before Complete; `zip64: auto`; the STORED + data-descriptor interop note (Java `ZipInputStream`); determinism holds within the same image (zlib version recorded).
- **ADR-0010 and ADR-0014:** dispatcher states; body-only custody snapshot; per-message receipts; poison messages.
- **ADR-0020 and op-catalog §3:** import allow-list; lint, not a sandbox; no plugin secrets (P3-7).
- **ADR-0023:** logger levels; hashed run_key and paths; audit store layout.
- **ADR-0005 and engine-selection:** P3-8; file paths; pricing placeholder; the read-back verify cost is noted for Phase 8 calibration.
- **Other docs:**
  - spike-plan: S6 at 2 GB locally, 10 GB in Phase 8.
  - risk-register: verification pointers for R1, R2, R3, R7, R8 and R13; new risks for SFTP no-clobber depending on the server, HTTPS evidence, and zip interop.
  - open-questions: Q13–Q16 closed with P3-3 to P3-6.
  - Package READMEs; `testing/README.md`; the overview phase table; `phase-3-design.md` with an as-built section.

## 6. Verification (Phase 3 acceptance)

**From `packages/`:**
- `uv sync --locked --all-packages`
- `uv run ruff check .` and `uv run ruff format --check .`
- `uv run mypy` (strict)
- `uv run pytest` on Python 3.13 locally and on 3.11 via `/usr/bin/python3.11`, matching the CI matrix
- `factory schema --check`

**End-to-end** (`test_rt_e2e_*`, in-process, stand-ins only, synthetic data):
1. **Dev, unmodified worked example** (x = S3, y = SFTP). Flow: dispatcher → t1 → t2 → t3. Checks:
   - identical zip bytes at x and y;
   - manifest sha256 per file equals the source;
   - folder structure relative to the prefix;
   - sidecars correct;
   - a rerun of each task does nothing;
   - redelivery is idempotent (412 handled);
   - the zip is byte-identical across two runs and across `PYTHONHASHSEED` 0 and 1.
2. **ppt bindings:** x = HTTPS sink (digest, read-back and size-only variants recorded in the receipt), y = S3 in another placement.
3. **Other starters:**
   - `rest_to_s3` filled (mock API → zip → `s3.put`);
   - `s3_to_s3_event` (`s3.copy`);
   - `table_to_s3_fivetran` (`fivetran.sync` against the mock).
4. **Drills** (parametrised):

   | Drill | Expected exit |
   |---|---|
   | Portal attack URIs (other host, other bucket, other study, `..`, `%2e`, IP, userinfo) | 77, nothing delivered, audit event written |
   | Wrong SFTP host key | 77 |
   | Bundle sha mismatch | 77 |
   | Untrusted TLS certificate | 77 |
   | Vault sealed | 75 |
   | Missing secret | 78 |
   | Tampered staged zip | 65 |
   | Listing mismatch | 65 |
   | Portal 429 | Retry-After honoured |
   | Portal 503 burst | breaker opens, 75 |
   | Portal schema drift | 78 |
   | x delivered, y fails transiently | retry completes y |
   | Image digest differs from the pin | 78 |

5. **Canary:** canary secret values (synthetic) are injected. Every log line, staged object, receipt, run-state item, audit event, handler payload, MWAA `conf` and stand-in store is scanned for the canary and its derived forms. Nothing may match apart from the expected auth headers inside the stand-ins.
6. **Subprocess:** one task runs through `python -m factory_runtime.run` (production entry, overrides impossible) against moto and the stand-ins.

**Other checks:**
- `factory plan study_pkg_delivery` → t1 lambda, t2 lambda, t3 eks_pod.
- `factory test study_pkg_delivery` → all owner test cases pass.
- `factory bundle` sha is stable across processes.
- Spike S6 (opt-in): about 2 GB streaming within the memory bound; the throughput number is recorded in the spike report.
- pf 0.1.0 stays green: pytest from the repo root (270 passed / 14 skipped) and dry run 91/91.
- Docs: internal link check.

**Finish:** commit and push to `feature/pipeline-factory` with no PR, then stop and present the Phase 3 review.

---

## As built (differences from this design, and why)

Short notes; the full ADR and doc amendments of §5 move to Phase 4 (scope cut agreed during Phase 3 to save time).

**Scope changes**
- M11 is minimal: the planner and `factory plan` exist; `factory bundle` and `factory test` move to Phase 5 (the
  logic exists: `factory_spec.bundle.build_bundle`, `factory_spec.owner_tests.run_owner_tests`).
- M12 is cut down: one end-to-end run of the worked example in dev (dispatcher → t1 → t2 → t3, x = S3, y = SFTP)
  plus three drills (portal attack URIs, wrong SFTP host key, bundle tampering), all exiting 77 with nothing
  delivered; the canary is a log scan for the synthetic secrets. Dropped: the ppt (HTTPS) run, the starter runs,
  the subprocess run of `python -m factory_runtime.run`. HTTPS, static puts, s3.copy, Fivetran and plugins are
  covered by op- and task-level tests instead (`test_rt_transports`, `test_rt_deliver`, `test_rt_fivetran`,
  `test_rt_plugin`).

**Behaviour added or changed while building**
- Receipts: `outcome: skipped` (optional slot left empty), `verification: none` (an HTTPS endpoint that answers
  neither HEAD nor GET), `files[]` for s3.copy; receipts are adopted on retry (first answer wins).
- Every delivery streams through a hash check that holds back the last chunk, so a staged package changed after its
  checksum never lands complete (`staged_package_changed`, 65).
- S3 delivery sets `ExpectedBucketOwner` when the placement account is known; non-team placements need
  `FACTORY_PLACEMENT_ACCOUNTS` (otherwise `placement_unknown`, 78). s3.copy is verified by size only (server-side
  copy, conditional on the listed ETag); a stronger server checksum is a Phase 8 item.
- Dispatcher: poison messages are quarantined in staging (`_quarantine/`) and acknowledged; the dispatcher does not
  write the run pin (the first task does), conf carries the bundle sha for traceability.
- sqs.read `in_task`: an empty queue ends the run as `no_work` (exit 0; later tasks end at once via a `NOWORK`
  run-state record).
- Plugins: the context records side effects (`reads_staging`, `reads_workdir`, `writes_workdir`) and enforces them
  at run time too, not only in tests; custom rules have no params in v1.
- Planner prices are decimal strings (no floats); pod cost is divided by an assumed node utilisation (0.35) so
  Lambda stays the cheaper choice for short work, as the decision matrix intends.

**Open items for Phase 4/5**
- `s3_event` triggers: s3.list expects a folder prefix; listing the single object named by the event needs a small
  change (found while reviewing the s3_to_s3_event starter).
- HTTPS POST endpoints: the upload goes to `<folder>/<name>`; an upload-to-folder variant is not modelled.
- ADR/doc amendments listed in §5.
