# Secret provider design

Decision records: [ADR-0008](../adr/0008-secret-provider.md) and [ADR-0012](../adr/0012-encryption.md) (encryption keys).

## 1. Principles

- **Specs hold references only.** No secret value appears in specs, code, logs, the Airflow UI, XCom, `dag_run.conf`,
  docs, Terraform state or LLM prompts.
- **Secrets are resolved at run time, inside the workload.** That means an EKS pod or Lambda running as its workload
  role. MWAA never resolves pipeline secrets.
- **Every access is audited by reference** (never by value).

## 2. Types

```python
class SecretRef(BaseModel):                      # in the spec: secrets.<name>
    provider: Literal["vault", "aws_secrets_manager"]
    path: str                                    # may contain {env} / {team}; resolved from the environment binding
    key: str                                     # field inside the secret (JSON key or KV field)
    version: int | str | None = None             # Vault KV version / SM VersionId or VersionStage
    kind: Literal["password", "ssh_private_key", "ssh_host_key", "tls_client_cert", "tls_client_key",
                  "ca_bundle", "oauth_client", "api_key", "aes_key", "generic"]
    rotation_overlap: timedelta = timedelta(hours=24)

class SecretValue:                               # runtime only
    """Masked repr/str; refuses pickle, copy and JSON serialisation; .reveal() only inside a `with` block."""
```

## 3. Interface and implementations

```python
class SecretProvider(Protocol):
    def get(self, ref: SecretRef, *, purpose: str, run_id: str, rule_id: str | None) -> SecretValue: ...
```

| Implementation | Details |
|---|---|
| `VaultProvider` (hvac 2.4) | **KV v2** (`read_secret_version`, with version support) and **Transit** (`encrypt` / `decrypt` / `datakey`, for `key_source: vault_transit`). **Auth:** AWS IAM method using the workload's own credentials (Pod Identity / IRSA / Lambda role) with the `X-Vault-AWS-IAM-Server-ID` header; Vault roles are bound to workload role ARNs. **AppRole** only for runners outside AWS: response-wrapped `secret_id`, CIDR-bound. Token renewal / re-login before TTL expiry; Enterprise namespaces per team if available. |
| `AwsSecretsManagerProvider` (boto3) | `GetSecretValue` with `VersionId` / `VersionStage`; JSON key extraction; endpoint through the VPC interface endpoint; KMS decrypt through the secret's key policy. |

**Decorators**, composed in this order: `Audited(Cached(Impl))`.

| Decorator | Behaviour |
|---|---|
| `Cached` | In-process only (never on disk), TTL per kind (default 5 min). **Rotation-safe:** an auth or decrypt failure while using a cached value invalidates the entry and refetches **once**. Rotation overlap must be greater than TTL + the longest task duration (validated against `orchestration` timeouts). |
| `Audited` | For every `get`, an audit event goes to the run log and the audit store: `{ts, principal_arn, provider, path, key, version_resolved, purpose, run_id, rule_id, pipeline_id, outcome, latency_ms}`. The value, and any hash of the value, are never recorded. |

**Masking:** every value revealed in a process is registered with the `factory_core.jsonlog` redaction filter,
which masks the whole value and each line of multi-line PEM values (the pf `_mask` pattern).

## 4. Where secrets are resolved

| Consumer | Resolves | Identity |
|---|---|---|
| EKS pod | source credentials, encryption keys, portal credentials | pipeline workload role (Pod Identity session tags or IRSA) |
| Lambda | dispatcher's portal credentials; small-op credentials | function role |
| MWAA | **only** Airflow Connections and Variables through the native Secrets Manager backend (`airflow/connections/*`) | execution role |
| CI (deploy) | Fivetran API key; Fivetran connector credentials, pushed via REST with `ignore_changes` in Terraform | team deploy role (GitHub OIDC) |

## 5. Encryption keys (summary of ADR-0012)

| `key_source` | Material | Recommended for |
|---|---|---|
| `kms` (default) | AWS Encryption SDK with a KMS keyring. The data key is generated per package; the plaintext key never leaves the process. | everything, unless consumers need another format |
| `vault_transit` | ESDK raw-AES keyring with the data key wrapped by Vault Transit. The master key never leaves Vault. | when Vault is mandated |
| `vault_kv` | Raw AES-256 key read from Vault KV (the brief's example). The key version is recorded in the manifest. | only when the consumer holds the same key; GxP warning |

- **Algorithm:** AES-256-GCM through the ESDK's committing algorithm suite (HKDF, key commitment), frame size
  ≥ 1 MiB. The unsigned suite is the default because integrity also comes from the checksum and Object Lock. The
  signed suite is available. The algorithm is declared in the spec and documented per pipeline.
- **OpenPGP** is available as an alternative `format` when the consumer requires it.

## 6. Pod hardening for secrets

- Secrets are never passed in pod args or environment variables. Pods fetch them at run time.
- Read-only root filesystem; tmpfs for transient key files (mTLS client keys), mode 0600, deleted after use.
- No shell or debugging tools in the runtime image.
- **Canary test (CI):** plant known fake secret values, run the pipeline tests, then scan logs, XCom, conf,
  generated files and docs for them. Any hit fails the build.

## 7. Testing

- **Unit:** `AwsSecretsManagerProvider` against moto; `VaultProvider` against a Vault **dev server** (KV v2 and
  Transit enabled) and with an hvac fake for error paths (sealed, 403, expired token, version not found).
- **Rotation:** a new version is written mid-run, and the cached value is invalidated on the auth failure.
- **Audit:** every `get` produces exactly one audit event with no value material.
