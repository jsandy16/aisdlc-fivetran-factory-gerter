# ADR-0008: Pluggable SecretProvider (Vault, Secrets Manager); resolved only in workloads; native SM backend for Airflow's own connections

- **Status:** Accepted
- **Date:** 2026-10-13

## Context

Secrets must be references only and must never appear in specs, code, logs, the UI, XCom, docs or prompts. Both Vault and Secrets Manager are required.

## Decision

- **Interface.** `SecretProvider.get(ref, purpose, run_id, rule_id) -> SecretValue`.
- **Implementations:**
  - `VaultProvider`: KV v2 and Transit; AWS IAM auth with the server-id header; AppRole for non-AWS runners.
  - `AwsSecretsManagerProvider`.
- **Decorators:** `Cached` (in-process TTL, rotation-safe refetch) and `Audited` (an event by reference, never the value).
- **`SecretValue`** has a masked repr, cannot be serialised, and every revealed value is registered with the redaction filter.
- **Where secrets are resolved.** Pipeline secrets are resolved only in pods and Lambdas. **No custom Airflow secrets backend.** MWAA's native `SecretsManagerBackend` serves only Airflow's own platform connections (`airflow/*`).
- **Fivetran** connector credentials are set via REST from CI, never in Terraform state.

## Consequences

- Vault stays out of Airflow's availability path.
- Secret access is attributable per run.
- A CI canary scan proves there are no leaks.

## Alternatives considered

- *A custom Airflow backend wrapping the provider (allowed by the brief):* rejected, because it would let the shared execution role read team secrets.
- *Injecting secrets as pod environment variables:* rejected, because they are visible in pod specs.

## Phase 3 amendments

- Vault via a built-in httpx client (AWS IAM login, KV v2 by version, renew/lookup, health); hvac removed (P3-1). Transit and AppRole deferred.
- Values are registered with the log redactor at construction, with derived forms (url-encoded, base64, JSON-escaped, Basic auth, access tokens). Team namespace for KV and auth mount (P3-6). Custom rules get no secrets (P3-7).
