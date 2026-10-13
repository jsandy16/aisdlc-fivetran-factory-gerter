# ADR-0013: Streaming packaging with checksums computed in flight and an independent re-check

- **Status:** Accepted
- **Date:** 2026-10-13
- **Amended:** MD5 over the zip; 10 GB max (D3, D10).

## Context

Packages are at most 10 GB per run (D10). The S3 ETag is not an MD5 for SSE-KMS or multipart objects. Checksums cannot be added to multipart object metadata after the upload starts.

## Decision

- **Streaming:** S3 objects are read as streams → deterministic zip (zip64, data descriptors, sorted entries, fixed metadata, `store` for compressed inputs) → S3 multipart (adaptive part size).
- **Checksums:** MD5 and SHA-256 **over the zip** while streaming.
- **Independent check:** `checksum.compute` re-reads the stored zip and compares with the manifest.
- **Where checksums live:** sidecar objects (`.md5`, `.sha256`) and the manifest are delivered with the zip.
- **Engines:** at 10 GB, streaming makes Lambda feasible for collect, checksum and deliver (the planner decides).

## Consequences

- Bounded memory, accurate checksums (ALCOA+ accuracy).

## Alternatives considered

- *Using the ETag as the checksum:* it is wrong for KMS/multipart objects.
