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

| Op | Category | Summary | Engines | Notes |
|---|---|---|---|---|
| `portal.lookup` | integration | Call a portal endpoint, validate it against `response_schema`, map it with JMESPath, snapshot write-once | lambda, eks_pod | never on MWAA (portal credentials) |
| `sqs.read` | integration | Read the trigger's message batch (snapshot) or, in `in_task` mode, receive → snapshot → dedupe → delete (custody transfer) | lambda, eks_pod | [ADR-0010](../adr/0010-event-triggers-and-sqs-custody.md) |
| `sqs.ack` | integration | Delete or acknowledge messages after custody | lambda, eks_pod | |
| `secret.read` | integration | Resolve a `SecretRef` to a handle for later rules (value never leaves the process) | lambda, eks_pod | |
| `rest.call` | extract / integration | Generic HTTPS call (auth, pagination, reliability as for the portal) | lambda, eks_pod | |
| `discover.list` | extract | List a source (absolute path or recursive) with include/exclude globs, readiness, `allowed_roots` | lambda, eks_pod | |
| `transfer.download` | extract | Download a `file_set` (or stream it) with size verification and retries | eks_pod (lambda if small) | reuses pf session code |
| `s3.list` / `s3.get` | extract | S3 sources, including versioned keys | lambda, eks_pod | |
| `package.zip` / `package.tar` | package | Deterministic archive preserving folder structure under a declared root; streaming; zip64 | eks_pod (lambda if small) | |
| `package.unzip` | transform | Safe extract (zip-slip, bomb, symlink, duplicate, encrypted defences from pf) | eks_pod, lambda | |
| `manifest.build` | package / validate | Manifest of path, size, sha256 (md5 optional), source mtime and package checksums; canonical JSON | any data engine | |
| `crypto.encrypt` / `crypto.decrypt` | encrypt | ESDK (KMS, raw AES from Vault KV, Vault Transit) or OpenPGP; streaming | eks_pod (lambda if small) | [ADR-0012](../adr/0012-encryption.md) |
| `checksum.compute` | checksum | md5 / sha256 / sha512 of a stored object; writes a sidecar | lambda (if feasible), eks_pod | [ADR-0013](../adr/0013-checksums-and-large-packages.md) |
| `validate.*` | validate | `file_count`, `total_size`, `manifest_match`, `name_pattern`, `checksum_match`, `schema` (CSV/JSON header), `expression` (CEL) | any data engine | severity + action (fail / quarantine / warn) |
| `naming.render` | naming | Render a name from a restricted token template (`{study_id}`, `{run_date}`, `{seq}`, `{source_system}`, `{rev}`) or a CEL naming rule | any | tokens are a fixed allow-list; no runtime Jinja |
| `s3.upload` | upload | Multipart, SSE-KMS, tags, `If-None-Match` write-once, checksum headers | lambda, eks_pod | |
| `s3.copy` / `s3.move` / `s3.rename` | upload / housekeeping | Server-side copy (UploadPartCopy above 5 GB), re-encrypts with the target key; move = copy + verify + delete (when allowed) | lambda, eks_pod | |
| `housekeeping.retention` | housekeeping | Tag or expire staging objects; never touches Object Lock targets | lambda | |

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
