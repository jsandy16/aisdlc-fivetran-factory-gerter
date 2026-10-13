# ADR-0012: AWS Encryption SDK with KMS by default; Vault Transit or Vault KV keys and OpenPGP as explicit options

- **Status:** Proposed (Phase 1 review)
- **Date:** 2026-10-13

## Context

The worked example reads an encryption key from a Vault entry. Envelope encryption with KMS is preferred. Packages can be very large, and the decryptor's tooling is unknown (Q2).

## Decision

- **`crypto.encrypt` parameters:** `format: aws_esdk | openpgp` and `key_source: kms | vault_transit | vault_kv`.
- **Default** is the ESDK with a KMS keyring: AES-256-GCM committing suite, HKDF, frames ≥ 1 MiB, unsigned by default with signing optional, streaming.
- **`vault_kv`** uses a raw AES keyring; the key version is recorded in the manifest. It triggers a warning under GxP.
- **`vault_transit`** wraps the data key so the key never leaves Vault.
- **Retries.** Encryption is nondeterministic, so targets are written once and later steps "adopt the winner".
- **gxp_direct** can add a decrypt-verify step.

## Consequences

- The algorithm is declared and documented.
- Large files are supported by streaming.
- The consumer must be able to decrypt the ESDK format (or choose OpenPGP).

## Alternatives considered

- *Raw AES-GCM over the whole file:* rejected, because of message-size limits and no framing.
