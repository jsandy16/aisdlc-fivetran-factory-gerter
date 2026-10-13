# Phase 2 design (spec models, schema, CLI, worked example)

Status: **approved with the plan for Phase 2 (2026-10-13)**. This is the design review produced before
implementation. Where the implementation differs, the code and these overrides win:

| Topic | Design text below | Decision applied |
|---|---|---|
| Rule category for delivery | renamed `upload` → `deliver` | **kept the brief's name `upload`**; the op is `destination.deliver` |
| Destination allow-list location | `environments.yaml` (platform-owned) | **separate `spec/destinations.yaml` owned by governance** (binding-type, part of `binding_sha`); see ADR-0003 and ADR-0024 |
| Environment list | `EnvName = Literal["dev","test","ppt","prod"]` | **read from `factory.toml` (`environments = [...]`)**; every listed env must be bound |
| `table_to_s3_fivetran` starter | candidate to cut | **kept, model-only** (`fivetran.sync`; REST provisioning in Phase 5) |

---

I read the Phase 1 docs and ADRs, the pf modules you named, the CI workflow and the root `pyproject.toml`. I also checked a few library behaviours against the pydantic 2.14.1 and ruamel.yaml 0.19.1 installed in `.venv`; those findings are marked **[verified]**.

## 0. Decisions this design takes (Phase 1 amendments shipped with Phase 2)

- **Destinations.**
  - The portal returns one URI per destination *slot* (`x`, `y`).
  - `destination.resolve` accepts a URI only if it matches exactly one entry in that environment's allow-list (`environments.yaml`).
  - Host, bucket and port must match exactly. The path is matched with a restricted pattern tied to the run's own values.
  - No match is a `security` error. This gets a new ADR-0024.
- **Encryption.**
  - The `encrypt` category and the `crypto.*` ops stay in the model with `status: planned`. Using one gives `op_not_available`.
  - `security.package_encryption` accepts only `none` for now.
  - MD5 is computed over the zip.
- **Portal.** The `portal.mode` field is removed; portal changes always take effect at run time.
- **Accounts.**
  - `account_model` has a single value, `account_per_team`. MWAA is a placement alias pointing at the platform account.
  - Environments are fixed to `dev | test | ppt | prod`.
- **Identity.** A platform setting `identity.default: pod_identity`, with one workload role per pipeline and no ABAC. This only affects Phase 5.
- **New concept: `variables`.** These are named run values, for example `study_id: trigger.message.study_id`. They are the only names that templates, path patterns and CEL `vars` may use.
- **Moves and renames.**
  - `data_classification` moves to `security.yaml`, because it is a governance decision.
  - The category `upload` is renamed `deliver`.
  - The key `from:` is renamed `via:`, because `from` is a Python keyword and would need aliases. The rule is: no aliases anywhere in the spec models.
- **What the lock does not contain.** No platform settings and no engine decisions. Engine decisions go into the Phase 3 plan file, so a change to the engine table never changes `logic_sha`.
- **Docs to amend in Phase 2:**
  - ADRs 0009, 0011, 0012, 0013, 0015 and 0021;
  - open-questions Q1–Q8 (now answered);
  - spike-plan (AWS-only spikes move to a new Phase 8);
  - spec-outline §2/§7/§8, op-catalog, engine-selection §3, the security doc §2/§6, and mwaa-strategy (central MWAA, cross-account `InvokeRestApi`).

---

## A. Packages and the uv workspace

### A.1 Where the workspace root goes

| Option | Effect on pf | Risk |
|---|---|---|
| Add `[tool.uv.workspace]` to the root `pyproject.toml` | pf becomes a workspace member, so `uv.lock` must resolve all of pf's extras together (fivetran-connector-sdk 2.12.1, claude-agent-sdk, moto[server], duckdb, psycopg2). pip ignores `tool.uv`, so `pip install -e` still works. | One lock couples pf's dependency resolution to the new packages; extras conflicts are likely; pf is installed in the new environment, so an accidental `import pf` passes |
| **Separate virtual workspace root at `packages/pyproject.toml` (recommended)** | none | Tool config discovery (below) |

Recommendation: the separate root.
- The root `pyproject.toml` and pf's CI job stay as they are.
- pf is **not installed** in the workspace environment, so "adapt, don't import" is enforced by the structure itself.
- The root can be moved up later, once pf is moved into `packages/pf`.

```toml
# packages/pyproject.toml  (virtual root: no [project])
[tool.uv]
required-version = ">=0.11.32,<0.12"
[tool.uv.workspace]
members = ["factory_core", "factory_spec", "factory"]   # explicit: factory_runtime/airflow have no pyproject yet → a glob would error
[tool.uv.sources]
factory-core = { workspace = true }
factory-spec = { workspace = true }
[dependency-groups]
dev = ["pytest", "hypothesis", "mypy", "ruff", "types-jmespath"]   # exact pins via uv.lock (tech-stack versions; verify)

[tool.pytest.ini_options]          # MUST be here, otherwise pytest walks up to pf's config (testpaths=["tests"])
testpaths = ["factory_core/tests", "factory_spec/tests", "factory/tests"]
addopts = "-q --strict-markers --import-mode=importlib"   # three tests/ dirs, no __init__ name clashes

[tool.mypy]
strict = true
python_version = "3.11"
plugins = ["pydantic.mypy"]
files = ["factory_core/src", "factory_spec/src", "factory/src"]      # strict on src; tests excluded
[tool.pydantic-mypy]
init_forbid_extra = true
init_typed = true
warn_required_dynamic_aliases = true
[[tool.mypy.overrides]]
module = ["celpy", "celpy.*"]
ignore_missing_imports = true

[tool.ruff]
line-length = 120
target-version = "py311"          # keeps ruff from suggesting 3.12-only syntax (PEP 695 etc.)
[tool.ruff.lint]
select = ["E","W","F","I","B","UP","SIM","RUF","S","PT","C4","DTZ","T20","TID","PERF","N"]
[tool.ruff.lint.per-file-ignores]
"*/tests/**" = ["S101","S603","S607"]
[tool.ruff.lint.flake8-tidy-imports.banned-api]
"pf".msg = "adapt pf code, never import it"
"pf_runtime".msg = "adapt pf code, never import it"
[tool.ruff.lint.flake8-type-checking]
runtime-evaluated-base-classes = ["pydantic.BaseModel", "factory_spec.common.StrictModel"]
```

**Member packages**
- Build backend: hatchling, with a src layout and a `py.typed` file.
- `requires-python = ">=3.11"`. All three packages share one version (`0.2.0.dev0`).
- Dependencies:
  - `factory-core`: `pydantic>=2.9,<3`, plus extras `yaml` (ruamel) and `cel` (cel-python==0.5.0). The extras keep `factory_core.errors` importable on MWAA later.
  - `factory-spec`: `factory-core[yaml,cel]`, `jmespath==1.1.0`, and an exact `pydantic==2.14.1` pin, because JSON Schema bytes depend on it. factory_spec never runs on MWAA.
  - `factory`: `factory-spec` and `typer` (0.27.x, to verify).

### A.2 `factory_core` (stdlib + pydantic; ruamel and cel loaded lazily)

```python
# errors.py
class ErrorClass(StrEnum): TRANSIENT="transient"; DATA="data"; CONFIG="config"; SECURITY="security"; INTERNAL="internal"
EXIT_CODE: Final = {TRANSIENT: 75, DATA: 65, CONFIG: 78, SECURITY: 77, INTERNAL: 70}   # 70 = EX_SOFTWARE: bugs page platform, no retry
class FactoryError(Exception):
    error_class: ClassVar[ErrorClass]
    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None: ...
class TransientError(FactoryError): ...   # likewise DataError, ConfigError, SecurityError
def exit_code_for(exc: BaseException | None) -> int: ...
# canonical.py
def canonical_bytes(doc: object) -> bytes            # sort_keys, (",",":"), ensure_ascii=False, allow_nan=False, rejects float & non-JSON types
def doc_sha256(kind: str, body: object, **header: str) -> str   # sha256(canonical({"kind":..,"v":1,**header,"body":..}))
def pretty_json(doc: object) -> str                  # indent=2, sort_keys, LF, trailing "\n"
# yamlio.py   (adapted from pf: round-trip, width=4096 so long scalars are never re-wrapped)
class YamlError(ValueError): code: str; line: int | None   # yaml_syntax|yaml_alias|yaml_tag|yaml_duplicate_key|yaml_key_type|yaml_date|yaml_too_large
def load_text(text: str, *, max_bytes: int = 1 << 20) -> CommentedMap
def dump_text(doc: Any) -> str ; def dump_path(doc: Any, path: Path) -> None   # temp file + os.replace
def to_plain(node: Any) -> JsonLike   # ScalarInt/Float/String → builtins; TaggedScalar / date / non-str key → YamlError
# envelope.py
class Issue(BaseModel): severity: Literal["error","warning"]="error"; file: str=""; loc: str=""; code: str; msg: str; hint: str | None = None
class Result(BaseModel): api_version: Literal["factory/v1"]; command: str; ok: bool; factory_version: str; issues: list[Issue]; data: dict[str, Any]
# cel.py      (pf_runtime/cel.py null-aware semantics) + def referenced_paths(expr: str) -> set[tuple[str, ...]]   # walks the lark AST (member_dot/ident)
# templates.py   class Template(parts: tuple[str | Token, ...]); def parse_template(s) -> Template; def render(t, values, *, segment_tokens) -> str
# uri.py         class DestinationUri(type, host, port, segments); def parse_destination_uri(s: str) -> DestinationUri   # raises SecurityError
# pathmatch.py   class PathPattern(...); def parse_path_pattern(s, *, absolute: bool); def match(p, segs, tokens) -> bool; def may_overlap(a, b) -> bool
# units.py       parse_bytes / parse_duration / format_*    (no retry/jsonlog in Phase 2: they have no consumer yet → Phase 3)
```

### A.3 `factory_spec`

```
factory_spec/
  common.py        StrictModel(extra=forbid, frozen, validate_default=True); REQUIRED; ba(label, help) -> FieldInfo; HashExclude marker
  types.py         Identifier, RuleId, TaskId, ArtifactName, SlotId, SemVer, EnvName=Literal["dev","test","ppt","prod"],
                   ByteSize, Duration, RetentionPeriod, Money, Glob, Regex, CelExpr, JmesPath, Hostname, S3Bucket, KmsAlias,
                   SortedSet[T], RunKeyTemplate, NamingTemplate, PathTemplate, SecretPathTemplate, VariableSource
  sections/        metadata, variables, trigger, orchestration, reliability, observability, workload, cost,
                   rules, integrations, portal, destinations, secrets, security, environments, platform
  ops/             *Params + *Impl per op (integration, s3, transfer, rest, package, manifest, checksum, validate,
                   naming, deliver, fivetran, planned)
  catalog/         contracts.py (OpContract, PortSpec, AwsActionTemplate, EgressTemplate, EngineResources)
                   op_catalog.v1.json, engine_table.v1.json, artifact_kinds.py (kind → CEL facets)
  files.py         SPEC_FILES = {"pipeline.yaml": PipelineFile, ...}; OWNER_OF_FILE
  spec.py          PipelineSpec(pipeline, rules, integrations, security, environments) + PrivateAttr indexes
  load.py          load_spec(dir, *, settings_dir) -> LoadResult; yaml_loc(); placeholders(); translate(pydantic_err)
  dynamic.py       build_model(name, ObjectSpec, *, unknown_fields) -> type[BaseModel]
  derive.py        derive_artifacts(spec, catalog) -> ArtifactGraph
  validate/        __init__ (CHECKS: tuple[Check, ...]); refs, dataflow, templates, triggers, sources, destinations,
                   portal, envs, gxp, engines, custom, secrets
  codes.py         CODES: dict[str, CodeInfo(severity, summary, hint)]   # every emitted code is registered
  lock.py          build_lock(spec, ctx) -> Lock; logic_sha(spec); binding_sha(spec, env); check_lock(dir) -> list[Issue]
  schema_export.py FactoryJsonSchema(GenerateJsonSchema); export_schemas() -> dict[str, str]
  schemas/         *.schema.json + SCHEMAS.json (committed; drift-checked)
  migrate/         Migration(from_version, to_version, description, apply(docs)); Registry.chain(); migrate_dir(...)
  patch.py         SpecPatch(file, op: set|add|remove, path, value); apply_patch(dir, patch, *, keep_invalid) -> PatchOutcome
  ba_view.py       render_ba(spec) -> BaDocument (numbered sections from x-ba-label)
```

- **Checks are plain functions:** `Check = Callable[[PipelineSpec, ValidationContext], Iterable[Issue]]`. Unlike pydantic validators, they collect every finding and can emit warnings.
- **Model validators** are only for local shape rules, for example p50 ≤ p95 ≤ max. They raise `PydanticCustomError(code, msg)` so the error code passes straight through.

### A.4 `factory` CLI (Typer)

- **Code layout:**
  - `cli.py` holds the app and `main(argv) -> int`. It runs `app(..., standalone_mode=False)` and turns `click.UsageError` into a `--json` envelope.
  - The app is created with `rich_markup_mode=None`, `pretty_exceptions_enable=False` and `add_completion=False`.
  - `repo.py` finds the repo root by the `factory.toml` marker.
  - `commands/{init,new,validate,schema,show,lock,migrate,edit}.py`.
  - The starters ship as **package data**, under `factory/starters/<name>/{starter.toml, spec/*.yaml}`. `starters/README.md` points there, which keeps wheel installs working.
- **Commands:**

  | Command | What it does |
  |---|---|
  | `factory [--json] init [PATH]` | Writes `factory.toml`, `pipelines/`, `.gitignore`, `.factory/schemas/` |
  | `new <id> --starter S [--team T]` | Copies the starter, then uses the patch engine to set `pipeline_id` and `team`. No text templating. |
  | `validate <id> [--allow-unfilled] [--platform-settings DIR]` | Validates the spec |
  | `schema [--write DIR] [--check]` | Writes or drift-checks the JSON Schemas |
  | `show <id> --view ba\|lock` | Plain-English view, or the computed lock |
  | `lock <id> [--check]` | **Added** (small). Writes the lock and gives CI a staleness check. |
  | `migrate <id> [--to V] [--dry-run]` | Runs chained schema migrations |
  | `set\|add\|remove <id> <file> <path> [value] [--keep-invalid]` | Structured edits with rollback |

- **Exit codes** stay pf's build-time convention: 0 ok, 1 findings, 2 usage or environment error. They are deliberately separate from the runtime 65/75/77/78.

### A.5 pf hygiene and CI

- **`tests/adversarial/test_cli.py`** lines 14 and 20:
  - Use `PF = str(Path(sys.executable).with_name("pf"))` and `PATH = str(Path(sys.executable).parent) + os.pathsep + ...`.
  - Note: `/home/claude/pipeline-factory/.venv/bin/pf` **exists in this container** **[verified]**. These 19 tests currently run a foreign virtualenv's `pf` entry point against this repo's `src`. Re-measure the 270/14 baseline after the fix.
- **Root `pyproject.toml`:** `requires-python = ">=3.11"`.
- **README install line:** add `-c examples/pipelines/oncology_labs/build/connector/requirements.txt`.
- **Untrack egg-info:** `git rm -r --cached src/pipeline_factory.egg-info`, then add `*.egg-info/`, `.mypy_cache/`, `.ruff_cache/` and `.pytest_cache/` to `.gitignore`.
- **New CI job `factory`** (existing job untouched):
  - Steps: checkout (SHA-pinned), then `pip install uv==0.11.32` (no new third-party action), then in `packages/`: `uv sync --locked --all-packages --python ${{matrix.py}}`, followed by `uv run --no-sync` for `ruff check .`, `ruff format --check .`, `mypy`, `pytest` and `factory schema --check`.
  - Then, in `examples/team-pipelines`: `../../packages/.venv/bin/factory --json validate study_pkg_delivery` and `... lock study_pkg_delivery --check`.
  - Matrix: `py: ["3.11", "3.12"]`.

---

## B. Spec model

**File → section map.** Each file has exactly one CODEOWNER, recorded as `x-owner-role` on the file's root schema.

| File | Sections | Owner |
|---|---|---|
| `pipeline.yaml` | `api_version`, `kind`, `schema_version`, `metadata`, `variables`, `trigger`, `orchestration`, `reliability`, `observability`, `workload`, `cost`, `artifacts` (overrides), `open_questions` | data engineer |
| `rules.yaml` | `rules[]` | business owner |
| `integrations.yaml` | `sources[]`, `portal`, `apis[]`, `destinations[]` (slots), `secrets{}` | data engineer |
| `security.yaml` | `security_and_access` | governance / QA |
| `environments.yaml` | `environments{dev,test,ppt,prod}` | platform |

**Merging and references.**
- `PipelineSpec` is built only when all five files parse.
- Every reference uses a stable id: rule_id, task_id, artifact, variable, secret, source, slot, endpoint, queue, channel, or allow-list entry id.
- Locations are rendered in **selector form**, for example `rules[r_zip].implementation.params.level`. The value can be pasted straight into `factory set`.

**(1) SQS trigger.**

```python
class FieldSpec(StrictModel):          # shared by the message schema, portal responses and manual conf; recursive, depth ≤ 4
    type: Literal["string","integer","boolean","enum","list","object"]
    required: bool = True; pattern: Regex | None = None; max_length: int | None = None
    minimum: int | None = None; maximum: int | None = None; values: list[str] | None = None  # enum
    items: "FieldSpec | None" = None; max_items: int | None = None; fields: "dict[FieldName, FieldSpec] | None" = None
class SqsTrigger(StrictModel):
    type: Literal["sqs"]; mode: Literal["dispatch","in_task"] = "dispatch"; queue: Identifier
    max_receive_count: int = 5; message: MessageSpec   # {envelope: raw|sns|eventbridge, unknown_fields: ignore|reject, fields}
Trigger = Annotated[ScheduleTrigger | SqsTrigger | S3EventTrigger | ManualTrigger, Field(discriminator="type")]
```

- `reliability.run_key` is a `RunKeyTemplate`. It may only use variables sourced from the trigger, because the dispatcher builds the run key before any portal call.
- Every such variable must have a `pattern`.
- The runtime `dag_run_id` is `sanitise(run_key)[:200] + "-" + sha256(run_key)[:12]`.

**(2) Portal.**
- `portal`: `ApiSpec`, made of:
  - `auth`: a union on `type` of oauth2_client_credentials, api_key or mtls, with secret names only;
  - `reliability`;
  - `endpoints[]`, each with `{id, method, path: PathTemplate, query: dict[str, Template], pagination: union on type of none|page|cursor|link_header, response: {unknown_fields, fields}, mapping}`.
- `mapping` = `{destinations: dict[SlotId, JmesPath], values: dict[Identifier, JmesPath], expect: [{path: JmesPath, equals: Template}]}`.
  - `expect` lets the spec require, for example, that the response's `study_id` equals the requested one, which guards against cross-study answers.
- `dynamic.build_model` generates the response model:
  - `create_model` iterates fields in sorted order;
  - internal names are `f0, f1, …` with `alias=<original>`, so no name can shadow a BaseModel attribute such as `schema` or `json`;
  - `ConfigDict(strict=True, extra=ignore|forbid, frozen=True)`; validation uses `model_validate_json`;
  - model names are deterministic (`DeliveryTargetsResponse_Targets_Item`);
  - the same builder serves the SQS message schema.

**(3) Destinations from the portal (security-critical).**
- **In `integrations.yaml`:** `DestinationSlot{id, label, resolve: PortalResolve{via: portal, endpoint} | StaticResolve{via: static, entry, path: PathTemplate}, allowed_types: SortedSet[s3|sftp|https], required}`.
- **In `environments.yaml`:** `destinations: list[AllowedDestination]`, a union on `type`:
  - `s3{id, slots, bucket, prefix_pattern, placement, kms_key, max_classification}`;
  - `sftp{id, slots, host, port=22, path_pattern, username, private_key_secret, host_key_fingerprint, max_classification, at_rest_attestation}`. The host key is pinned **in the reviewed binding**, not in a secret store, so a key change shows up in a PR.
  - `https{…, method: PUT|POST, auth: bearer|oauth2|mtls, write_once: if_none_match|none, ca_bundle_secret?}`.
- **`parse_destination_uri` rejects:**
  - schemes other than s3, sftp or https;
  - non-ASCII input (no IDNs, which blocks homographs);
  - `%` anywhere (no percent-encoding, so `%2e%2e` and `%2F` tricks fail);
  - userinfo (`@`), query, fragment, `;`, backslash, control characters;
  - empty, `.` or `..` path segments, and segments outside `[A-Za-z0-9._-]`;
  - IP-literal hosts and trailing-dot hosts.
  
  It lowercases the host and fills in default ports.
- **Path pattern grammar:** segments are a literal, `{var}`, `*` (exactly one segment) or `**` (only as the last segment).
  - A `{var}` must **equal the run's own value**, so study A can never be routed to study B's folder.
  - At least one leading literal segment is required.
- **Matching:**
  - the type, the host (or the bucket for S3) and the port must be equal;
  - the slot must be listed in the entry's `slots`;
  - the type must be in the slot's `allowed_types`;
  - exactly one entry may match.
  
  No match gives `SecurityError("destination_not_allowed")`; two matches give a config error, but the static ambiguity check should already have prevented that.
- **What the generators get:** credentials, IAM resources (from each entry's literal root) and egress hosts all come from the entries. They are therefore finite and known at build time.

**(4) Rules.**
- The shared base `_RuleBase` has:
  - `rule_id`, `plain_english` (at least 10 characters), `inputs`, `outputs`;
  - `preconditions: list[CelExpr]`;
  - `on_failure{action: fail|quarantine|warn, error_class: data|config|security}`;
  - `test_cases[{name, given, expect}]` and `requirements`.
- Each category is its own model with `category: Literal[...]`, and `Rule = Annotated[Union[...10 categories], Field(discriminator="category")]`.
- The implementation union is nested, with an explicit `kind` (no short form):

```python
class PackageZipImpl(StrictModel): kind: Literal["builtin"]; op: Literal["package.zip"]; params: PackageZipParams
class PlannedOpImpl(StrictModel):  kind: Literal["builtin"]; op: Literal["crypto.encrypt","crypto.decrypt", ...]; params: dict[str, JsonValue] = {}
class ExpressionImpl(StrictModel): kind: Literal["expression"]; cel: CelExpr                       # validate, naming only
class CustomImpl(StrictModel):     kind: Literal["custom"]; code_sha256: Sha256Hex; output_kinds: dict[ArtifactName, ArtifactKind]
PackageImpl = Annotated[Union[Annotated[Union[PackageZipImpl, ManifestBuildImpl, PlannedOpImpl], Field(discriminator="op")],
                              CustomImpl], Field(discriminator="kind")]
```

- Nested discriminators work, and the error locations include all three tags **[verified]**. For example: `('rules',0,'package','implementation','builtin','package.zip','params','level')`.
- A wrong op gives `union_tag_invalid` with a `ctx.tag`, which is translated into `op_category_mismatch` or `op_unknown` with the list of allowed ops.
- A consistency test checks that the category unions match the catalog's `categories`.

**(5) Tasks.**
- `Task{task_id, label, rules: list[RuleId], depends_on, engine: Literal["auto","lambda","eks_pod","eks_pod_spot","mwaa","fivetran_native","fivetran_sdk"] = "auto", timeout: Duration, retry: RetryPolicy | None}`.
- `DagSettings{catchup, max_active_runs, timeout}`.
- `branch` and `parallel_group` are deferred to a later minor version.

**(6) Artifacts.** `derive_artifacts` works as follows:
1. Outputs are bound to contract output ports by position. Inputs are bound by **kind**, so their order doesn't matter.
2. There must be exactly one producer per name, and it must sit earlier in the same task or in an ancestor task.
3. Durability:
   - `staged` when the artifact crosses a task boundary, or when the contract sets `min_durability: staged` (message, portal_snapshot, object_listing, destination_set, manifest, checksum);
   - otherwise `stream` when both ports support streaming;
   - otherwise `local`.
   
   Delivery receipts are terminal.
4. `pipeline.yaml: artifacts: {name: {durability}}` may only *raise* durability.
5. The graph goes into the lock under `derived.artifacts`.

CEL artifact facets come from `artifact_kinds.py`, for example object_listing exposes `{count, total_bytes}`. The CEL roots are `artifacts`, `vars` and `run`.

**(7) `<REQUIRED>` placeholders.**
- `_placeholders` is reused on plain data.
- `yaml_loc` generalises pf's `_loc`. At a dict node it drops a location part when `part not in node` and `part == node.get(k)` for `k in ("category","kind","op","type","via")`. This handles the three consecutive tags.
- An error whose location is equal to, a prefix of, or an extension of a placeholder location is suppressed. So `type: <REQUIRED>`, which gives `union_tag_invalid` at the parent, is suppressed.
- Avoid non-discriminated (smart) unions. They inject type names such as `str` into error locations.
- Cross-file checks run only once every file parses.
- `lock` refuses any spec that still has `required` issues.

**(8) Units.**
- `ByteSize`:
  - Accepts `^\d+(\.\d+)?\s?(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$`, case-sensitive, and stores int bytes. `Decimal` is used, so the result must be integral.
  - Bare integers are rejected with the hint "add a unit".
  - Don't use `pydantic.ByteSize`: it is case-insensitive and accepts floats **[verified: `10gib` and `1.5GB` accepted]**.
  - Document that `10GB` means 10¹⁰ bytes, while Lambda `/tmp` is 10 GiB.
- `Duration`: `^(\d+d)?(\d+h)?(\d+m)?(\d+s)?$`, stored as int seconds. Without a custom serializer a timedelta dumps as `"PT15M"` **[verified]**.
- `RetentionPeriod`: `^\d+(d|y)$`, kept as a string.
- `Money`: Decimal quantized to 0.01 and serialised as `"2.00"`.
- Defaults are written in input form (`Field(default="15m")` with `validate_default=True`), so the JSON Schema shows `"15m"`.

**(9) Token templates.**
- Syntax: `{name}`, plus `{seq:0Nd}` / `{rev:0Nd}` for integers only; `{{` and `}}` escape braces.
- Parsing is a hand-written scanner. `str.format` must never be used: it allows `{x.__class__}`.
- Allowed tokens by context:

  | Context | Allowed tokens |
  |---|---|
  | run_key | trigger-sourced variables, `pipeline_id` |
  | naming | variables, `pipeline_id`, `team`, `env`, `run_date`, `seq`, `rev`, `artifacts.<value>` |
  | portal path/query | variables, `env`, `pipeline_id` |
  | destination patterns | variables, `env`, `team`, `pipeline_id` |
  | secret paths | `env`, `team`, `pipeline_id` |

- In path contexts, variables must be pattern-constrained (`template_token_unconstrained`).
- At run time each substituted value must match `^[A-Za-z0-9][A-Za-z0-9._-]*$`.

---

## C. Op contract catalog v1 (`catalog/op_catalog.v1.json`, validated by `OpContract`)

Each entry has these fields:
- `op`, `version`, `status`, `categories`, `inputs[]` / `outputs[]` (`{port, kind, required, streaming, min_durability}`);
- `allowed_engines`, `side_effects`, `aws_actions[{actions, resource}]`, `egress[{host, port}]`;
- `determinism`, `idempotency`, `classification`;
- `resources{engine: {bytes_per_s, memory, streams, overhead_s}}`, `params_model`;
- `contract_sha`, which is computed, not authored.

The template resource vocabulary is `{staging.run_prefix_arn}`, `{source.<id>.bucket_arn}`, `{entry.bucket_arn}`, `{entry.literal_root}`, `{queue.<n>.arn}`, `{secret.<n>.arn}`, `{kms.<alias>.arn}` and `{run_state.table_arn}`.

| Op | Cat. | In → Out | Engines | Side effects | aws_actions (abridged) | Egress | Det. | Idem. |
|---|---|---|---|---|---|---|---|---|
| `sqs.read` | integration | – → message | λ, pod | reads_staging; in_task: consumes_queue | S3 Get staging; in_task: sqs Receive/Delete/ChangeVisibility, ddb PutItem | – | det_effect | dedupe_key |
| `portal.lookup` | integration | message? → portal_snapshot | λ, pod | calls_portal, reads_secret, writes_staging | SM GetSecretValue; S3 Put staging; KMS | `{portal.host}:443` | det_effect | conditional_write |
| `destination.resolve` | integration | portal_snapshot, message → destination_set | λ, pod | reads/writes_staging | staging | – | pure | conditional_write |
| `s3.list` | extract | message? → object_listing | λ, pod | reads_source, writes_staging | `s3:ListBucket` on source bucket, cond. `s3:prefix` | – | det_effect | natural |
| `s3.download` | extract | object_listing → file_set (stream) | λ, pod | reads_source | `s3:GetObject[Version]`, `kms:Decrypt` | – | pure | natural |
| `discover.list` / `transfer.download` | extract | – → object_listing → file_set | pod, λ | reads_source, reads_secret | secrets | `{source.host}:{port}` | det_effect / pure | natural |
| `rest.fetch` | extract | message? → file_set | λ, pod | calls_api, reads_secret, writes_staging | secrets, staging | `{api.host}:443` | det_effect | conditional_write |
| `package.zip` | package | file_set → package | pod, λ | writes_staging | S3 Put/AbortMultipart staging, KMS | – | pure | conditional_write |
| `manifest.build` | package, validate | package, object_listing? → manifest | λ, pod | writes_staging | staging | – | pure | conditional_write |
| `checksum.compute` | checksum | package, manifest? → checksum (+ sidecars) | λ, pod | reads/writes_staging | staging | – | pure | conditional_write |
| `validate.file_count` / `total_size` / `manifest_match` | validate | listing/manifest → report | λ, pod | – | staging read | – | pure | natural |
| `naming.render` | naming | – → value | λ, pod | allocates_sequence (only with `{seq}`) | ddb UpdateItem run_state | – | det_effect | dedupe_key |
| `destination.deliver` | deliver | package, manifest?, checksum?, value, destination_set → delivery_receipt | λ, pod | reads_staging, writes_target, reads_secret | **expanded per allow-list entry of the slot**: s3 → Put/AbortMultipart on `{entry.bucket_arn}/{entry.literal_root}*`, KMS on `{entry.kms}`; sftp/https → secrets | sftp/https entries' host:port | det_effect | conditional_write (S3 `If-None-Match`; SFTP temp name + no-clobber rename; HTTPS per `write_once`) |
| `s3.put`, `s3.copy`, `sftp.put`, `https.put` | deliver (copy also housekeeping) | artifact → receipt (static slots) | λ, pod | writes_target (+ reads_secret) | as above; copy adds source GetObject | entry host | det_effect | conditional_write |
| `fivetran.sync` | integration | – → report | mwaa | triggers_external_sync | – (Airflow connection; REST API) | api.fivetran.com:443 | nondeterministic | natural |
| **planned:** `crypto.encrypt`, `crypto.decrypt`, `package.tar`, `package.unzip`, `s3.move`, `s3.rename`, `sqs.ack`, `secret.read`, `housekeeping.retention`, `validate.schema`, `validate.name_pattern`, `validate.checksum_match` | encrypt / … | — | — | — | — | — | — | — |

- **Resource model.** These are placeholders until spike S6, now in Phase 8:
  - Lambda: S3 read 90 MB/s; deflate 60 MB/s per core; store 150 MB/s; `sftp.put` 25 MB/s; `https.put` 40 MB/s; `s3.copy` moves no bytes through the function.
  - Pods: roughly 2× the Lambda read figure.
- **Engine table.** `engine_table.v1.json` holds the Lambda limits `{timeout: 900 s, memory: 10 GiB, tmp: 10 GiB, safety: 2, overhead: 10 s}`.

---

## D. Validators and error codes

Every code is registered in `codes.py` with a plain-English hint. A test checks that every error-severity code has at least one invalid-corpus case.

| Group | Codes |
|---|---|
| Load | `missing_file`, `yaml_syntax`, `yaml_alias`, `yaml_tag`, `yaml_duplicate_key`, `yaml_date` ("quote dates"), `required`, `required_field`, `unknown_field` (with did-you-mean), `invalid_value`, `invalid_choice`, `template_unquoted` (string field received `{x: null}`) **[verified: unquoted `{request_id}` parses as a mapping]**, `quote_value` (int where a string is expected, e.g. `001234` → 1234 **[verified]**), `api_version_mismatch`, `schema_version_unsupported`, `schema_version_outdated`, `pipeline_id_folder_mismatch`, `open_questions_pending` |
| Ops | `op_unknown`, `op_category_mismatch`, `op_not_available` (planned op; this replaces "encrypt needs key", and `encrypt_without_key` stays reserved), `implementation_kind_not_allowed`, `params_artifact_not_input` |
| References | `duplicate_id`, `unresolved_reference`, `rule_unassigned`, `rule_assigned_twice`, `task_cycle`, `secret_kind_mismatch`, `secret_key_required`, `secret_unused` (warning, evaluated across all envs), `variable_unresolvable`, `secret_in_spec` (PEM / AKIA / JWT patterns, from pf's `redact_secrets`) |
| Dataflow | `dataflow_gap`, `dataflow_multiple_producers`, `dataflow_order`, `artifact_kind_mismatch`, `output_arity_mismatch`, `dataflow_not_staged`, `final_write_outside_deliver`, `artifact_unused` (warning) |
| Expressions | `template_syntax`, `template_unknown_token`, `template_token_not_allowed`, `template_token_unconstrained`, `run_key_invalid`, `cel_invalid`, `cel_unknown_name` (AST paths checked against vars and kind facets), `jmespath_invalid`, `jmespath_unknown_field` (warning) |
| Trigger / sources | `event_trigger_without_queue` (sqs or s3_event lacks `queues.<q>` in an enabled env), `trigger_field_missing` (`bucket_field`/`prefix_field` not a required string field), `recursive_without_include` (`s3.list`/`discover.list` with `recursive: true` and an empty `include`; there is no implicit `**/*`), `source_unconstrained` (`via: trigger` without `buckets` + `prefix_roots` in an env) |
| Portal / destinations | `portal_required`, `destination_slot_unmapped`, `destination_slot_unbound` (a slot with no allow-list entry in an enabled env), `destination_entry_unknown` (static), `destination_static_path_not_allowed`, `destination_type_not_allowed`, `destination_pattern_too_broad`, `destination_host_invalid`, `destination_ambiguous` (`may_overlap`), `classification_target_mismatch`, `https_required` |
| Engines | `engine_not_allowed` (not in the intersection of the ops' `allowed_engines`; mwaa running data ops; fivetran engines on anything but `fivetran.sync`), `task_timeout_exceeds_engine`, `engine_infeasible` (error when the estimate exceeds the limit even without the safety factor), `engine_marginal` (warning inside the safety band; the constants are unvalidated until Phase 8) |
| GxP (tier ≠ non_gxp) | `gxp_spot_forbidden`, `gxp_audit_missing`, `gxp_sha256_missing` (policy requires sha256, a `manifest.build` with sha256 exists, and every deliver rule has `manifest: true`), `gxp_encryption_at_rest_missing` (`s3_destinations ≠ sse_kms`), `gxp_external_destination_unattested` (sftp/https entry without `at_rest_attestation`), `checksum_policy_mismatch`, `retention_shorter_than_replay` |
| Environments / platform | `env_missing`, `env_unknown` (all 4 envs must be present), `binding_missing`, `env_resource_shared` (the same bucket or queue in two envs, which means cross-environment leakage), `unknown_placement_alias`, `unknown_team`, `platform_settings_missing` (warning; platform checks skipped), `workload_inconsistent`, `workload_exceeds_limit` (`max` > `limits.max_package_bytes` = 10 GB) |
| Custom rules | `custom_rule_code_missing`, `custom_rule_code_mismatch`, `custom_rule_untested`, `custom_rule_unreviewed` (Phase 2 checks that `impl.lock.json` exists with a matching sha; the digest content is Phase 4) |
| Lock | `lock_missing`, `lock_stale` |

**Lambda feasibility (explicit `engine: lambda` only).** This is the Phase 2 rule:
- **Time:** consecutive streaming ops are fused, so each segment takes `bytes_max / min(bytes_per_s)`. The estimate is the sum over segments plus per-op overheads. It is feasible if `safety × est ≤ 900`.
- **Disk:** zero if every byte-moving op streams. Otherwise it is bytes × the number of materialisations, which must fit in 10 GiB.
- **Where destination types are chosen at run time:** use the worst-case throughput over the slot's entries in the **union of all environments**. This keeps the engine choice identical across environments.

Worked numbers:
- **t2_checksum at 10 GB:** 10e9/90e6 + 10 = 121 s, ×2 = 242 s. Passes.
- **t1 on Lambda:** streaming list → download → zip gives ≈ 354 s ×2, so it is feasible only because it streams.
- **t3 with two SFTP slots:** about 820 s, ×2 = 1,640 s, so `engine_marginal`. This is why t3 is set to `auto`.

---

## E. Lock and hashing

```json
{ "lock_version": 1, "api_version": "factory/v1", "schema_version": "1.0.0", "pipeline_id": "study_pkg_delivery",
  "spec_version": "0.1.0",
  "logic":    { "pipeline": {...}, "rules": [...sorted by rule_id], "integrations": {...}, "security": {...} },
  "bindings": { "dev": {...}, "test": {...}, "ppt": {...}, "prod": {...} },
  "derived":  { "artifacts": {...}, "op_contracts": {"package.zip": {"version": "1.0.0", "contract_sha": "…"}} },
  "hashes":   { "logic_sha": "…", "binding_sha": {"dev": "…", ...}, "derived_sha": "…" } }
```

**What goes into each hash.**
- `logic_sha = doc_sha256("factory.logic", logic)`, over the normalised `pipeline`, `rules`, `integrations` and `security`.
  - Fields marked `HashExclude` are left out: `metadata.{name, description, owners, tags, requirements, spec_version}` and every `label`.
  - `plain_english` and `test_cases` **are included**. They are the approved acceptance criteria, and ADR-0020's digest binds them.
- `binding_sha[env] = doc_sha256("factory.binding", binding[env], env=env, pipeline_id=…)`.
  - Changing one environment changes only that environment's hash.
- Platform settings and engine decisions are **not** in the lock. Their hashes belong in BUILD-MANIFEST (Phase 3+), so platform edits don't make every team's lock stale.

**Normalisation steps.**
- Use `model_dump(mode="json", warnings="error")` with `exclude_unset`, `exclude_defaults` and `exclude_none` all False, so defaults are filled in.
- Short forms are expanded in validators.
- Set-like lists (include/exclude, slots, algorithms, scopes) are sorted and de-duplicated by `SortedSet`.
- Id-keyed lists (rules, sources, slots, endpoints, allow-list entries) are sorted by id in the lock. The YAML keeps the author's order.
- `tasks` are sorted by task_id. Generators order them topologically, breaking ties by task_id.

**Pitfalls.**
- **Python mode leaks objects.** `mode="python"` dumps keep `Decimal`, `timedelta` and `date` objects. pf's `canonical(default=str)` would turn them into silent strings (as it would a ruamel `TaggedScalar` reaching a `dict[str, Any]` field), so `canonical_bytes` rejects non-JSON types.
- **No floats in hashed documents.** ByteSize becomes int, Duration int seconds, Money a `"x.yy"` string. A float found in a hashed document is a bug.
- **Decimal formatting.** `Decimal("5.00")` and `Decimal("5")` serialise differently, and `normalize()` produces `5E+2`, so quantize instead.
- **AnyUrl rewrites values.** `AnyUrl` lowercases the host and adds a trailing slash **[verified]**. Use plain validated `str` for every hashed URL.
- **No aliases.** With no aliases anywhere, `by_alias` cannot go wrong. Avoid computed fields too.
- **Hash the canonical form, not the file.** Hashes are taken over compact canonical sub-documents, never over the pretty-printed lock file.
- **Lock file text.** Written with `pretty_json` and LF. It contains no timestamps or paths and never its own hash.
- **Unicode.** Strings are not NFC-normalised (documented). YAML already normalises line breaks.
- **Schema migrations change `logic_sha`.** Because defaults are filled in, a minor schema bump that adds a defaulted field changes `logic_sha`. Accept and document this: only `factory migrate` changes `schema_version`, and new defaults must preserve behaviour.

---

## F. Deterministic JSON Schema export

**Generator settings.**
- Subclass `FactoryJsonSchema(GenerateJsonSchema)`:
  - `field_title_should_be_set → False`;
  - strip model `title` keys outside of `properties` maps;
  - add `$schema` (2020-12) and a constant URN `$id`.
- Generate with `mode="validation"`, `ref_template="#/$defs/{model}"`.
- Write with `json.dumps(sort_keys=True, indent=2, ensure_ascii=False) + "\n"`.

**Pitfalls.**
- **Nested discriminated unions.** pydantic writes a `discriminator.mapping` whose `builtin` value is an *inline object*, not a string **[verified]**. That is invalid OpenAPI and duplicates the inner `oneOf`. Post-process to drop `discriminator` keywords that aren't flat (or all of them; editors use the `const` in each `oneOf` branch).
- **`$defs` names come from class names.** Name collisions turn into module-qualified names, so a test asserts that all class names are unique.
- **pydantic version drift.** Minor and patch releases have changed the output (`const`, `$ref` siblings, `examples`). Hence the exact pin, plus `SCHEMAS.json {pydantic, files: {name: sha}}`. On drift, `--check` prints the pydantic version in use.
- **Custom types need explicit input forms.** Use `WithJsonSchema` to show what users can type, e.g. `{"oneOf": [{"type": "integer"}, {"type": "string", "pattern": …}]}`.
- **Regex dialects.** Patterns must be ECMA-262-compatible (no `(?P<`, `\A`, `\Z` or lookbehind). pydantic-core also validates with the Rust regex engine (no lookaround). A test checks every exported pattern.
- **Docstrings become descriptions.** Keep docstrings BA-facing and put developer notes in comments.
- **Labels.**
  - Field level: `Annotated[T, ba("Pipeline id", "…")]`, where `ba` returns `Field(description=…, json_schema_extra={"x-ba-label": …})`. Keeping the label in `Annotated` metadata leaves the defaults visible to the mypy plugin.
  - Model and file level: `model_config = ConfigDict(json_schema_extra={"x-ba-label": …, "x-owner-role": …})`.
  - Planned ops: `"x-status": "planned"`.
  - Test: every property has an `x-ba-label`, except discriminator fields.
- **Byte stability across processes.** Export in two subprocesses with `PYTHONHASHSEED=0` and `=1` and compare the bytes.

---

## G. Test plan

- **`factory_core` unit tests:**
  - canonical: key-order invariance (Hypothesis), float/NaN/date rejection;
  - yamlio: aliases, duplicate keys, `!!python/` and `!custom` tags **[verified: these load as `TaggedScalar`]**, dates, non-string keys, size cap;
  - CEL null semantics (ported from pf) and `referenced_paths`;
  - templates: escapes, format specs, `str.format`-style attack strings;
  - URI and path-pattern table, at least 40 adversarial cases: `..`, `%2e%2e`, `%2F`, `good.com@evil.com`, `https://evil@good.com`, `good.com.evil.com`, trailing dot, Cyrillic homograph, IPv4/IPv6 literals, odd ports, `//`, backslash, query/fragment, `{study_id}` mismatch, `**` depth;
  - a Hypothesis property: whenever `match` succeeds, the path starts with the literal root and contains no `..`.
- **Invalid-spec corpus**, under `factory_spec/tests/corpus/invalid/NNN_<code>.yaml`:
  ```yaml
  description: prod has no queue binding for the sqs trigger
  base: worked_example                       # or starters/<name> + fill file
  patches: [{file: environments.yaml, op: remove, path: environments.prod.queues.package_ready}]
  expect:  [{code: event_trigger_without_queue, file: environments.yaml, loc: environments.prod}]
  exact: false
  ```
  - The runner copies the base to `tmp_path`, applies `SpecPatch` with `keep_invalid=True`, validates, and checks that every expected (code, file, location prefix) appears and that each error has a non-empty hint.
  - A `valid/` corpus holds mutations that must stay valid *and hash-stable*:
    - reordering rules, secrets or allow-list entries keeps `logic_sha` and `binding_sha`;
    - `10GB` vs `10000MB` and `15m` vs `900s` hash the same;
    - an owner change keeps `logic_sha`;
    - a prod-only change moves only `binding_sha.prod`.
- **Round trip:**
  - every example and starter YAML satisfies `dump(load(x)) == x` byte for byte, which doubles as a format check;
  - `set` changes exactly one line (difflib) and keeps comments;
  - `remove` on a commented key is covered by a golden test.
- **Lock:** the golden `spec.lock.json` for the worked example is regenerated and compared, and `lock --check` runs in CI.
- **Schema drift:** `factory schema --check`, plus the two-process determinism test.
- **Migrations:** a dummy `1.0.0 → 1.1.0` that adds a defaulted field is registered **only in a test registry**. Golden directories `golden/1.0.0` and `golden/1.1.0` show comments preserved. The chain and the outdated/unsupported version errors are tested.
- **Dynamic models:** the portal and message specs produce stable JSON Schemas; tests cover strict JSON-mode validation, extra-field policy, alias handling and shadowing names (`schema`, `json`).
- **CLI:**
  - `typer.testing.CliRunner` in-process, one test per command, asserting exactly one JSON document on stdout and the exit code;
  - with Click 8.2+ there is no `mix_stderr`, so read `result.stdout`;
  - usage errors in `--json` mode also produce an envelope;
  - one subprocess smoke test of the `factory` console script.
- **Starters:**
  - each starter, filled from `tests/starters/<name>.fill.yaml`, validates clean;
  - `new --starter` followed by `validate --allow-unfilled` reports only `required` issues.
- **mypy:**
  - `model_validator(mode="after")` returns `Self`;
  - field validators take `@classmethod` with typed values;
  - `create_model` returns `type[BaseModel]`;
  - ruamel returns `Any`, so `warn_return_any` needs casts in yamlio;
  - ruff's TC rules must not move pydantic annotation imports under `TYPE_CHECKING`.

---

## H. Worked example: `examples/team-pipelines/pipelines/study_pkg_delivery/spec/`

`examples/team-pipelines/factory.toml`: `api_version = "factory/v1"`, `team = "clinops_data"`, `platform_settings = "../../platform/platform-settings"`.

```yaml
# pipeline.yaml (owner: data engineer). BA view: factory show study_pkg_delivery --view ba
api_version: factory/v1
kind: Pipeline
schema_version: 1.0.0
metadata:
  pipeline_id: study_pkg_delivery
  name: Study package delivery
  description: >-
    When a study package has been uploaded to the clinops landing bucket, zip it with a manifest,
    checksum the zip, and deliver it to the two destinations (x and y) the self-service portal names.
  team: clinops_data
  spec_version: 0.1.0
  owners: {business_owner: "@alice-ba", data_engineer: "@bob-de", governance: "@carol-qa", platform: "@dan-platform"}
  requirements: [REQ-001, REQ-002, REQ-003, REQ-004, REQ-005]
  tags: {cost_centre: CC1234}
variables:                          # the only names templates, destination patterns and CEL `vars` may use
  request_id: trigger.message.request_id
  study_id: trigger.message.study_id
  package_id: trigger.message.package_id
  expected_files: trigger.message.file_count
  expected_bytes: trigger.message.total_bytes
trigger:
  type: sqs
  mode: dispatch                    # Lambda snapshots + dedupes, then InvokeRestApi with dag_run_id = run_key
  queue: package_ready
  max_receive_count: 5
  message:
    envelope: raw
    unknown_fields: ignore
    fields:
      request_id: {type: string, pattern: "^[A-Za-z0-9-]{8,64}$"}
      study_id: {type: string, pattern: "^[A-Z]{3}[0-9]{3,5}$"}
      package_id: {type: string, pattern: "^PKG[0-9]{4,8}$"}
      bucket: {type: string, pattern: "^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"}
      prefix: {type: string, max_length: 900}
      file_count: {type: integer, minimum: 1, maximum: 20000}
      total_bytes: {type: integer, minimum: 1}
      uploaded_by: {type: string, required: false}
orchestration:
  dag: {catchup: false, max_active_runs: 4, timeout: 4h}
  tasks:
    - task_id: t1_collect
      label: Read the request, resolve destinations, zip the package
      rules:
        - r_read_request
        - r_portal_lookup
        - r_resolve_destinations
        - r_list_objects
        - r_check_listing
        - r_download
        - r_zip
        - r_manifest
      engine: auto
      timeout: 90m
    - task_id: t2_checksum
      label: Checksum the stored zip
      depends_on: [t1_collect]
      rules: [r_checksum]
      engine: lambda                # explicit: must pass the feasibility check at workload.max
      timeout: 15m
    - task_id: t3_deliver
      label: Deliver to destinations x and y
      depends_on: [t2_checksum]
      rules: [r_package_name, r_deliver_x, r_deliver_y]
      engine: auto
      timeout: 2h
reliability:
  run_key: "{request_id}"
  retry: {max_attempts: 3, backoff: exponential_jitter, base: 10s, max: 5m}
  replay_window: 30d
observability:
  alerts: {transient: none, data: clinops_data, config: platform, security: security}
  sla: 3h
workload:
  bytes_per_run: {p50: 1GB, p95: 6GB, max: 10GB}
  files_per_run: {p50: 400, p95: 5000, max: 20000}
  runs_per_day: 6
cost: {budget_per_run_usd: 2.00, budget_per_month_usd: 300.00}
open_questions: []
```

```yaml
# rules.yaml (owner: business owner)
rules:
  - rule_id: r_read_request
    category: integration
    plain_english: Take the package-ready message that started this run, exactly as received.
    implementation: {kind: builtin, op: sqs.read, params: {}}
    outputs: [request]
    requirements: [REQ-001]
  - rule_id: r_portal_lookup
    category: integration
    plain_english: Ask the self-service portal where this study's packages go (destinations x and y).
    implementation: {kind: builtin, op: portal.lookup, params: {endpoint: delivery_targets}}
    inputs: [request]
    outputs: [portal_answer]
    on_failure: {action: fail, error_class: config}
    requirements: [REQ-004]
  - rule_id: r_resolve_destinations
    category: integration
    plain_english: >-
      Accept each destination from the portal only if it matches a destination pre-approved for this
      environment and this study; otherwise stop the run and alert security.
    implementation: {kind: builtin, op: destination.resolve, params: {slots: [x, y]}}
    inputs: [portal_answer, request]
    outputs: [destinations]
    on_failure: {action: fail, error_class: security}
    requirements: [REQ-004]
    test_cases:
      - name: approved_destinations
        given: {env: test, vars: {study_id: ONC101}, portal: {x: "s3://acme-clinops-delivery-test/studies/ONC101/in/", y: "sftp://sftp-stub.test.acme.example/inbound/ONC101/"}}
        expect: {x: test_delivery_s3, y: test_stub_sftp}
      - name: other_study_rejected
        given: {env: test, vars: {study_id: ONC101}, portal: {x: "s3://acme-clinops-delivery-test/studies/ONC999/in/", y: "sftp://sftp-stub.test.acme.example/inbound/ONC101/"}}
        expect: {error_class: security, code: destination_not_allowed}
  - rule_id: r_list_objects
    category: extract
    plain_english: List every object under the bucket and folder named in the message, including sub-folders.
    implementation:
      kind: builtin
      op: s3.list
      params: {source: landing, recursive: true, include: ["**/*"], exclude: ["**/.DS_Store", "**/Thumbs.db"], max_objects: 20000, max_total_bytes: 10GB}
    inputs: [request]
    outputs: [listing]
    requirements: [REQ-001]
  - rule_id: r_check_listing
    category: validate
    plain_english: The number of files and their total size must equal what the message announced.
    implementation:
      kind: expression
      cel: "artifacts.listing.count == vars.expected_files && artifacts.listing.total_bytes == vars.expected_bytes"
    inputs: [listing]
    on_failure: {action: fail, error_class: data}
    requirements: [REQ-002]
    test_cases:
      - name: complete_upload
        given: {artifacts: {listing: {count: 3, total_bytes: 1200}}, vars: {expected_files: 3, expected_bytes: 1200}}
        expect: {result: pass}
      - name: missing_file
        given: {artifacts: {listing: {count: 2, total_bytes: 800}}, vars: {expected_files: 3, expected_bytes: 1200}}
        expect: {result: fail}
  - rule_id: r_download
    category: extract
    plain_english: Read each listed object as a stream, checking its size and ETag against the listing.
    implementation: {kind: builtin, op: s3.download, params: {verify: size_and_etag, concurrency: 8}}
    inputs: [listing]
    outputs: [files]
  - rule_id: r_zip
    category: package
    plain_english: >-
      Zip all files, keeping their folder structure below the message's folder, in a fixed order so the
      same files always give the same zip.
    implementation:
      kind: builtin
      op: package.zip
      params: {paths: relative_to_prefix, compression: deflate, level: 6, store_if_compressed: true, zip64: auto}
    inputs: [files]
    outputs: [package]
    requirements: [REQ-003]
  - rule_id: r_manifest
    category: package
    plain_english: List every file in the zip with its path, size and SHA-256, plus the zip's own MD5 and SHA-256.
    implementation:
      kind: builtin
      op: manifest.build
      params: {file_algorithms: [sha256], package_algorithms: [md5, sha256], format: json}
    inputs: [package, listing]
    outputs: [manifest]
    requirements: [REQ-003]
  - rule_id: r_checksum
    category: checksum
    plain_english: Re-read the stored zip and compute its MD5 and SHA-256; both must equal the values recorded while zipping.
    implementation: {kind: builtin, op: checksum.compute, params: {algorithms: [md5, sha256], compare_with_manifest: true}}
    inputs: [package, manifest]
    outputs: [package_checksum]
    on_failure: {action: fail, error_class: data}
    requirements: [REQ-003]
  - rule_id: r_package_name
    category: naming
    plain_english: Name the delivered zip <study>_<package>.zip, for example ONC101_PKG0042.zip.
    implementation: {kind: builtin, op: naming.render, params: {template: "{study_id}_{package_id}.zip"}}
    outputs: [package_name]
    test_cases:
      - name: example
        given: {vars: {study_id: ONC101, package_id: PKG0042}}
        expect: {value: ONC101_PKG0042.zip}
  - rule_id: r_deliver_x
    category: deliver
    plain_english: >-
      Deliver the zip, its manifest and its .md5 and .sha256 files to destination x; never overwrite a
      file that is already there, and re-check the delivered zip.
    implementation:
      kind: builtin
      op: destination.deliver
      params: {slot: x, object_name: package_name, manifest: true, sidecars: [md5, sha256], verify_after_write: true}
    inputs: [package, manifest, package_checksum, package_name, destinations]
    outputs: [delivered_x]
    requirements: [REQ-005]
  - rule_id: r_deliver_y
    category: deliver
    plain_english: Deliver the same files to destination y, under the same rules as destination x.
    implementation:
      kind: builtin
      op: destination.deliver
      params: {slot: y, object_name: package_name, manifest: true, sidecars: [md5, sha256], verify_after_write: true}
    inputs: [package, manifest, package_checksum, package_name, destinations]
    outputs: [delivered_y]
    requirements: [REQ-005]
```

```yaml
# integrations.yaml (owner: data engineer). Concrete buckets, hosts and URLs live in environments.yaml.
sources:
  - id: landing
    type: s3
    label: Clinops landing bucket (uploaded packages)
    location: {via: trigger, bucket_field: bucket, prefix_field: prefix}
portal:
  label: Self-service portal
  auth: {type: oauth2_client_credentials, token_path: /oauth2/token, client: portal_client, scopes: [delivery-targets.read]}
  reliability:
    connect_timeout: 5s
    read_timeout: 30s
    max_attempts: 5
    backoff_base: 1s
    backoff_max: 30s
    retry_on_status: [429, 500, 502, 503, 504]
    honour_retry_after: true
    circuit_breaker: {failure_threshold: 5, reset_after: 60s}
    rate_limit_per_second: 5
  endpoints:
    - id: delivery_targets
      method: GET
      path: "/api/v1/studies/{study_id}/delivery-targets"
      query: {status: active}
      pagination: {type: none}
      response:
        unknown_fields: ignore
        fields:
          study_id: {type: string}
          targets:
            type: list
            max_items: 10
            items:
              type: object
              fields:
                slot: {type: enum, values: [x, y]}
                uri: {type: string, max_length: 1024}
                active: {type: boolean}
      mapping:
        destinations:
          x: "targets[?slot=='x' && active] | [0].uri"
          y: "targets[?slot=='y' && active] | [0].uri"
        expect:
          - {path: study_id, equals: "{study_id}"}    # the portal must answer for the study we asked about
destinations:
  - id: x
    label: Destination x (primary delivery, chosen in the portal)
    resolve: {via: portal, endpoint: delivery_targets}
    allowed_types: [https, s3, sftp]
    required: true
  - id: y
    label: Destination y (archive copy, chosen in the portal)
    resolve: {via: portal, endpoint: delivery_targets}
    allowed_types: [s3, sftp]
    required: true
secrets:
  portal_client: {provider: aws_secrets_manager, path: "clinops/{env}/portal-client", kind: oauth_client}
  partner_sftp_key: {provider: vault, path: "clinops/{env}/partner-sftp", key: private_key, kind: ssh_private_key}
  cro_upload_token: {provider: aws_secrets_manager, path: "clinops/{env}/cro-upload", key: token, kind: api_key}
```

```yaml
# security.yaml (owner: governance / QA)
security_and_access:
  gxp_tier: gxp_indirect
  data_classification: restricted
  encryption_at_rest: {staging: sse_kms, s3_destinations: sse_kms}
  package_encryption: none          # application-level encryption is done outside the pipeline for now (ADR-0012, deferred)
  checksums: {manifest: [sha256], delivery_sidecars: [md5, sha256]}
  audit:
    enabled: true
    events: [delivery, destination_resolution, portal_call, rule_result, secret_access]
    evidence_retention: 10y
  retention: {staging_after_success: 7d, staging_after_failure: 30d, run_logs: 400d}
  residency: {allowed_regions: [eu-west-1]}
```

```yaml
# environments.yaml (owner: platform). No account ids: placements are aliases in platform/platform-settings/<env>.json.
# `destinations` is the allow-list: the portal can only choose among these.
environments:
  dev:
    enabled: true
    placement: {orchestration: mwaa, workload: team}
    secrets: {vault: {mount: kv, namespace: clinops}, aws_secrets_manager: {prefix: factory/}}
    queues: {package_ready: {name: clinops-package-ready-dev}}
    sources:
      landing: {buckets: [acme-clinops-landing-dev], prefix_roots: [incoming/], placement: team}
    portal: {base_url: "https://portal.dev.acme.example"}
    alert_channels:
      clinops_data: {sns_topic: clinops-data-alerts-dev}
      platform: {sns_topic: factory-platform-alerts-dev}
      security: {sns_topic: factory-security-alerts-dev}
    destinations:
      - {id: dev_delivery_s3, type: s3, slots: [x, y], bucket: acme-clinops-delivery-dev, prefix_pattern: "studies/{study_id}/**", placement: team, kms_key: alias/clinops-delivery-dev, max_classification: restricted}
      - {id: dev_stub_sftp, type: sftp, slots: [x, y], host: sftp-stub.dev.acme.example, port: 22, path_pattern: "/inbound/{study_id}/**", username: factory-delivery, private_key_secret: partner_sftp_key, host_key_fingerprint: "SHA256:q3Xb0d7Lr9mT2vY8kP4sW6nF1hJ5cA0eG7uI3oR9zDk", max_classification: restricted, at_rest_attestation: DEV-STUB-NOT-GXP}
  test:
    enabled: true
    placement: {orchestration: mwaa, workload: team}
    secrets: {vault: {mount: kv, namespace: clinops}, aws_secrets_manager: {prefix: factory/}}
    queues: {package_ready: {name: clinops-package-ready-test}}
    sources:
      landing: {buckets: [acme-clinops-landing-test], prefix_roots: [incoming/], placement: team}
    portal: {base_url: "https://portal.test.acme.example"}
    alert_channels:
      clinops_data: {sns_topic: clinops-data-alerts-test}
      platform: {sns_topic: factory-platform-alerts-test}
      security: {sns_topic: factory-security-alerts-test}
    destinations:
      - {id: test_delivery_s3, type: s3, slots: [x, y], bucket: acme-clinops-delivery-test, prefix_pattern: "studies/{study_id}/**", placement: team, kms_key: alias/clinops-delivery-test, max_classification: restricted}
      - {id: test_stub_sftp, type: sftp, slots: [x, y], host: sftp-stub.test.acme.example, port: 22, path_pattern: "/inbound/{study_id}/**", username: factory-delivery, private_key_secret: partner_sftp_key, host_key_fingerprint: "SHA256:H8sK2mP5qT9wX3zB6dF1gJ4lN7rV0yC2eA5uI8oE3kM", max_classification: restricted, at_rest_attestation: TEST-STUB-NOT-GXP}
  ppt:
    enabled: true
    placement: {orchestration: mwaa, workload: team}
    secrets: {vault: {mount: kv, namespace: clinops}, aws_secrets_manager: {prefix: factory/}}
    queues: {package_ready: {name: clinops-package-ready-ppt}}
    sources:
      landing: {buckets: [acme-clinops-landing-ppt], prefix_roots: [incoming/], placement: team}
    portal: {base_url: "https://portal.ppt.acme.example"}
    alert_channels:
      clinops_data: {sns_topic: clinops-data-alerts-ppt}
      platform: {sns_topic: factory-platform-alerts-ppt}
      security: {sns_topic: factory-security-alerts-ppt}
    destinations:
      - {id: cro_archive_s3, type: s3, slots: [x, y], bucket: acme-cro-archive-ppt, prefix_pattern: "studies/{study_id}/packages/**", placement: cro_archive, kms_key: alias/cro-archive-inbound, max_classification: restricted}
      - {id: partner_sftp, type: sftp, slots: [x, y], host: sftp-uat.partner-cro.example.com, port: 22, path_pattern: "/inbound/acme/{study_id}/**", username: acme-delivery-uat, private_key_secret: partner_sftp_key, host_key_fingerprint: "SHA256:pL4nQ8rS2tU6vW0xY3zA7bC1dE5fG9hI2jK6lM0nO4p", max_classification: restricted, at_rest_attestation: QA-ATT-2026-017}
      - {id: cro_https_upload, type: https, slots: [x], host: upload-uat.partner-cro.example.com, port: 443, path_pattern: "/api/v2/studies/{study_id}/packages/*", method: PUT, auth: {type: bearer, token_secret: cro_upload_token}, write_once: if_none_match, max_classification: restricted, at_rest_attestation: QA-ATT-2026-018}
  prod:
    enabled: true
    placement: {orchestration: mwaa, workload: team}
    secrets: {vault: {mount: kv, namespace: clinops}, aws_secrets_manager: {prefix: factory/}}
    queues: {package_ready: {name: clinops-package-ready-prod}}
    sources:
      landing: {buckets: [acme-clinops-landing-prod], prefix_roots: [incoming/], placement: team}
    portal: {base_url: "https://portal.acme.example"}
    alert_channels:
      clinops_data: {sns_topic: clinops-data-alerts-prod}
      platform: {sns_topic: factory-platform-alerts-prod}
      security: {sns_topic: factory-security-alerts-prod}
    destinations:
      - {id: cro_archive_s3, type: s3, slots: [x, y], bucket: acme-cro-archive-prod, prefix_pattern: "studies/{study_id}/packages/**", placement: cro_archive, kms_key: alias/cro-archive-inbound, max_classification: restricted}
      - {id: partner_sftp, type: sftp, slots: [x, y], host: sftp.partner-cro.example.com, port: 22, path_pattern: "/inbound/acme/{study_id}/**", username: acme-delivery, private_key_secret: partner_sftp_key, host_key_fingerprint: "SHA256:Tz7Yx3Wv9Ut5Sr1Qp8On4Ml0Kj6Ih2Gf9Ed5Cb1Aa3Z", max_classification: restricted, at_rest_attestation: QA-ATT-2026-017}
      - {id: cro_https_upload, type: https, slots: [x], host: upload.partner-cro.example.com, port: 443, path_pattern: "/api/v2/studies/{study_id}/packages/*", method: PUT, auth: {type: bearer, token_secret: cro_upload_token}, write_once: if_none_match, max_classification: restricted, at_rest_attestation: QA-ATT-2026-018}
```

`platform/platform-settings/dev.json` (ppt and prod also add an account `cro_archive` with id `30000000000N` and a placement `cro_archive: {account: cro_archive, role: destination}`):

```json
{"settings_version": 1, "environment": "dev", "region": "eu-west-1", "account_model": "account_per_team",
 "identity": {"default": "pod_identity", "allowed": ["irsa", "pod_identity"]},
 "accounts": {"platform": {"account_id": "100000000001"}, "clinops_data": {"account_id": "200000000001"}},
 "placements": {"mwaa": {"account": "platform", "role": "orchestration"}, "team": {"account": "@team", "role": "workload"}},
 "mwaa": {"environment_name": "factory-dev", "airflow_version": "3.3.1", "rbac_role_pattern": "team_{team}",
          "invoke_role_pattern": "factory-mwaa-invoke-{team}", "launcher_role_pattern": "factory-launcher-{team}"},
 "teams": {"clinops_data": {"account": "clinops_data",
   "eks": {"cluster": "factory-clinops-data-dev", "namespace": "team-clinops-data"},
   "stores": {"staging_bucket": "factory-clinops-data-dev-staging", "run_state_table": "factory-clinops-data-dev-run-state",
              "bundle_store": "factory-clinops-data-dev-bundles", "audit_store": "factory-clinops-data-dev-audit"},
   "kms_aliases": {"staging": "alias/factory-clinops-data-staging", "audit": "alias/factory-clinops-data-audit"}}},
 "limits": {"max_package_bytes": "10GB", "lambda": {"timeout": "15m", "memory": "10240MiB", "ephemeral_storage": "10240MiB"}},
 "vault": {"address": "https://vault.dev.acme.example:8200", "auth": {"method": "aws", "mount": "aws"}}}
```

**Starters**, as package data, each with `<REQUIRED>` holes and a fill file for tests:
- `s3_package_deliver`: the worked example with holes.
- `s3_to_s3_event`: `s3_event` dispatch, `s3.copy` to a static slot, one task on `auto`.
- `rest_to_s3`: schedule trigger, `apis[]` with cursor pagination, `rest.fetch` then `s3.put` to a static slot.
- `table_to_s3_fivetran`: schedule trigger, `fivetran.sync` on `fivetran_native`, connection provisioned through the REST API.

---

## Risks and gotchas

1. **Who approves the destination allow-list.** It lives in `environments.yaml`, which platform owns, but approving destinations is a governance decision and CODEOWNERS cannot split one file by key. Require a governance review on that file through branch protection, or move the allow-list into `spec/destinations.yaml` later.
2. **Engine choice must not depend on the environment.** Runtime-chosen destination types mean feasibility must use the union of all environments' allow-lists. Otherwise dev (S3 only) and prod (SFTP) would plan different engines for the same `logic_sha`.
3. **IAM and egress are scoped to the allow-list, not to a single run.** This is acceptable but should be documented. It is still far tighter than "any host".
4. **Control inputs are untrusted.** SQS fields (bucket, prefix) and portal URIs are attacker-influenced. Defences: the source allow-list, `{var}`-bound patterns, the `expect` study-id check, rejecting percent-encoding and non-ASCII input, and snapshotting per run. Record "portal changes take effect at run time under GxP" as an accepted risk.
5. **Schema output depends on the pydantic version.** Pin pydantic exactly in factory_spec. Team CI must use the toolchain image, or `schema --check` will fail spuriously.
6. **ruamel quirks:**
   - unquoted templates parse as mappings, dates as `date`, `001234` as an int;
   - tags load as `TaggedScalar`;
   - the default width re-wraps long scalars, so use width=4096 and translate these cases into friendly codes.
7. **Feasibility constants are unvalidated until Phase 8.** That is why `engine_marginal` is a warning and only clear overruns are errors.
8. **pytest, ruff and mypy discover their config by walking up.** Always run them from `packages/`, never run `uv` at the repo root, and list workspace members explicitly.
9. **Test-path rewiring.** The `test_cli` fix changes which pf binary runs (see A.5). Re-baseline 270/14.
10. **cel-python 0.5.0.** The AST walk depends on its lark tree shape; keep it pinned.
11. **Two expression languages (CEL and templates) may confuse BAs.** CEL is limited to validate/naming expressions and preconditions; templates use variables only.

## Scope cuts to keep Phase 2 shippable

- **Cut first:** the `table_to_s3_fivetran` starter. Ship it model-only or move it to Phase 5, where the Fivetran REST provisioning is designed.
- **Defer:**
  - `factory_core.retry` and `jsonlog` to Phase 3;
  - the `auto` engine planner and `engine_decisions` to Phase 3 (Phase 2 checks explicit engines only);
  - `portal_schedule` and `upstream` triggers, and `branch`/`parallel_group`;
  - acceptance-digest content to Phase 4;
  - residency checks to Phase 5;
  - mock-portal generation to Phase 3;
  - CODEOWNERS generation to Phase 5;
  - a `factory catalog` command (optional).
- **Params models that may slip:** `discover.list` and `transfer.download` (SFTP/FTPS *sources*). Their contract rows stay.
- **Add:** `factory lock` (small).

## Order of work

1. Hygiene fixes, the workspace skeleton and the CI job (green with empty tests).
2. `factory_core`.
3. Types, models, op params and the catalog JSON, with consistency tests.
4. The loader: `yaml_loc`, error translation, codes.
5. Worked example and platform settings, used to drive the models.
6. Validators, growing the invalid corpus alongside them.
7. Lock and golden lock.
8. Schema export and the drift check.
9. Migrations.
10. CLI and patch engine.
11. Starters.
12. BA view and walkthrough.

The ADR and doc amendments can run in parallel from day 1.

