# ADR-0013: Streaming packaging with checksums computed in flight and an independent re-check

- **Status:** Proposed (Phase 1 review)
- **Date:** 2026-10-13

## Context

Packages may reach hundreds of GB. The S3 ETag is not an MD5 for SSE-KMS or multipart objects. Checksums cannot be added to multipart object metadata after the upload starts.

## Decision

- **Streaming path:** source `open_stream()` → deterministic zip (zip64, data descriptors, sorted entries, fixed metadata, `store` for compressed inputs) → ESDK → S3 multipart (adaptive part size).
- **Checksums in flight:** MD5 and SHA-256 over plaintext and ciphertext.
- **Independent check:** a later `checksum.compute` re-reads the stored object.
- **Where checksums live:** sidecar objects and the manifest.
- **Planner:** sizes pods and deadlines, and suggests fusing tasks.

## Consequences

- Bounded memory, accurate checksums, ALCOA+ accuracy.
- Very large packages may take hours; on-demand EKS is used.

## Alternatives considered

- *Download everything to disk, then zip:* rejected, because of disk sizing and time.
- *Using the ETag as the checksum:* rejected, because it is wrong for KMS/multipart objects.
