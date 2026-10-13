# Built-in operation catalog and custom-rule contract

Built-in ops are the default; custom code is the escape hatch. Every op, built-in or custom, has a **contract**. The
contract is the spine of the framework, because one declaration drives:
- validation (allowed params, inputs and outputs, engines);
- IAM generation (resource-scoped actions);
- network allow-lists (declared egress);
- the engine planner (resource model, limits);
- cost (throughput per GB);
- docs (generated op descriptions).

## 1. Op contract (Phase 3 implements it; Phase 2 models it)

| Field | Meaning |
|---|---|
| `op` | dotted name, e.g. `package.zip` |
| `version`, `contract_sha` | semver of the op; hash of the contract (part of the build hash) |
| `category` | which rule categories may use it |
| `params` | Pydantic model (discriminated by `op`) |
| `inputs` / `outputs` | typed artefact kinds and cardinality |
| `allowed_engines` | subset of `lambda`, `eks_pod`, `mwaa` (control ops only) |
| `side_effects` | e.g. `reads_source`, `writes_staging`, `writes_target`, `reads_secret`, `calls_portal`, `deletes_message` |
| `aws_actions` | IAM action templates scoped to declared resources, e.g. `s3:PutObject` on `{target.bucket_arn}/{target.prefix}*` |
| `egress` | `host:port` templates resolved from bindings → security groups, Network Firewall FQDN allow-list, NetworkPolicy |
| `determinism` | `pure`, `deterministic_effect` or `nondeterministic` (encryption) |
| `idempotency` | `natural`, `conditional_write` or `dedupe_key` |
| `resources` | CPU, memory, disk and throughput (MB/s) per engine, for the planner and cost model |
| `max_input` | per-engine hard limit (e.g. Lambda /tmp) |
| `classification` | `preserves`, `may_reduce` or `may_raise`: how the op can change the classification of the data it handles |

## 2. Built-in ops (v1)

The machine-readable catalog is `packages/factory_spec/src/factory_spec/catalog/op_catalog.v1.json`, validated by
`OpContract`. Ops marked *planned* are in the schema but not yet implementable: using one gives
`op_not_available`.

| Op | Category | In → Out | Engines | Notes |
|---|---|---|---|---|
| `sqs.read` | integration | – → message | lambda, eks_pod | dispatch mode reads the custody snapshot; in_task mode receives → snapshots → dedupes → deletes ([ADR-0010](../adr/0010-event-triggers-and-sqs-custody.md)) |
| `portal.lookup` | integration | message? → portal_snapshot | lambda, eks_pod | validates against the generated response model; JMESPath mapping; `expect` guards; write-once snapshot; never on MWAA |
| `destination.resolve` | integration | portal_snapshot, message → destination_set | lambda, eks_pod | matches each slot's URI against the governance allow-list ([ADR-0024](../adr/0024-portal-resolved-destinations.md)) |
| `s3.list` | extract | message? → object_listing | lambda, eks_pod | bucket/prefix from the message, constrained by the source allow-list; recursive + include/exclude; limits |
| `s3.download` | extract | object_listing → file_set (stream) | lambda, eks_pod | streaming; size + ETag verified |
| `rest.fetch` | extract | message? → file_set | lambda, eks_pod | paginated HTTPS API (auth and reliability as for the portal) |
| `package.zip` | package | file_set → package | lambda, eks_pod | deterministic, streaming, zip64, folder structure relative to the prefix |
| `manifest.build` | package, validate | package, object_listing? → manifest | lambda, eks_pod | path, size, sha256 per file; package md5/sha256 |
| `checksum.compute` | checksum | package, manifest? → checksum | lambda, eks_pod | re-reads the stored object; compares with the manifest; writes sidecars |
| `validate.file_count` / `validate.total_size` / `validate.manifest_match` | validate | listing/manifest → report | lambda, eks_pod | severity + action (fail / quarantine / warn) |
| `naming.render` | naming | – → value | any data engine | restricted token template; `{seq}` allocated transactionally |
| `destination.deliver` | upload | package, manifest?, checksum?, value, destination_set → receipt | lambda, eks_pod | per matched entry: S3 (`If-None-Match`, SSE-KMS), SFTP (pinned host key, temp name + no-clobber rename), HTTPS (`write_once`); verify after write |
| `s3.put` / `s3.copy` / `sftp.put` / `https.put` | upload (copy also housekeeping) | artifact → receipt | lambda, eks_pod | static slots |
| `fivetran.sync` | integration | – → report | mwaa | triggers a Fivetran sync (REST API, D9) |
| *planned:* `crypto.encrypt`, `crypto.decrypt`, `package.tar`, `package.unzip`, `s3.move`, `s3.rename`, `sqs.ack`, `secret.read`, `housekeeping.retention`, `validate.schema`, `validate.name_pattern`, `validate.checksum_match`, `discover.list`, `transfer.download` | — | — | — | — |

## 3. Custom-rule plugin interface ([ADR-0020](../adr/0020-custom-rule-plugins.md))

```python
# rules/<rule_id>/impl.py: shape only, implemented in Phase 3
class Params(PluginParams):          # pydantic, extra="forbid"
    ...
class Inputs(PluginInputs):          # typed artefact handles
    package: PackageRef
class Outputs(PluginOutputs):
    renamed: PackageRef

class Rule(CustomRule[Params, Inputs, Outputs]):
    side_effects = {"writes_staging"}           # declared; checked against ctx calls in tests
    aws_actions: tuple[str, ...] = ()           # extra IAM needs (rare), rendered into the role
    def run(self, ctx: RuleContext, params: Params, inputs: Inputs) -> Outputs: ...
```

**What `RuleContext` gives a plugin:**
- `workdir`;
- streaming readers and writers for declared artefacts;
- `secret(name)`, which returns a `SecretValue` handle **only for secrets the rule declares**;
- `log`;
- `run_key` / `run_id`;
- `now()` (pinned per run).

**What a plugin cannot do:**
- import `boto3`, `hvac`, `socket`, `subprocess`, `os.environ` or `requests` (AST allow-list, reused from
  `pf/generate/connector.check_imports`);
- read files outside its workdir;
- open undeclared network connections (a default-deny NetworkPolicy and egress allow-list enforce this at run time).

**What must exist before a plugin is accepted:**
- `test_impl.py` (BA test cases + agent edge cases) passing;
- mutmut over a threshold;
- `mypy --strict` and bandit clean;
- a review record;
- a person's acceptance digest over `(contract, plain_english, code_sha256, tests_sha256)`.
