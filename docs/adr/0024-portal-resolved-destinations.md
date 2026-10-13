# ADR-0024: Portal-resolved destinations constrained by a governance-owned allow-list

- **Status:** Accepted
- **Date:** 2026-10-13

## Context

The portal names the destination(s) of each package at run time: another S3 bucket/prefix or an external server such as SFTP or HTTPS (D2), effective immediately (D4). An unconstrained portal answer would be a direct data-exfiltration path.

## Decision

- **Slots.** Destinations are *slots* (e.g. `x`, `y`) declared in `integrations.yaml` with `resolve: {via: portal, endpoint}` or `{via: static, entry, path}` and `allowed_types`.
- **Allow-list.** `destinations.yaml` (governance-owned, per environment) lists typed entries:
  - `s3`: bucket, `prefix_pattern`, KMS key, placement;
  - `sftp`: host, port, `path_pattern`, username, key secret, **pinned host-key fingerprint**, at-rest attestation;
  - `https`: host, port, `path_pattern`, PUT/POST, auth, `write_once`, at-rest attestation.
- **Strict URI parsing.** The portal's URI is rejected for:
  - a scheme other than s3/sftp/https;
  - non-ASCII characters (no IDN homographs) or any `%`;
  - userinfo, query, fragment, `;`, backslash or control characters;
  - empty, `.` or `..` segments;
  - IP literals or trailing dots.
- **Matching.** Type, host/bucket and port must be equal, and the slot must be allowed. The path must match the
  pattern (literal, `{var}`, `*`, trailing `**`), with every `{var}` **equal to the run's own value** and at least
  one leading literal. Exactly one entry must match; otherwise it is `security: destination_not_allowed`.
- **Derived from the entry, never from the portal:** credentials, KMS keys, IAM resources and egress rules.
- **Static checks:**
  - `destination_pattern_too_broad`;
  - `destination_ambiguous` (overlapping entries);
  - `destination_slot_unbound`;
  - classification ceilings per entry;
  - external entries require an at-rest attestation under GxP.

## Consequences

- The portal can choose among approved destinations immediately, but it cannot create new ones.
- IAM and egress are scoped to the allow-list, which is finite and known at build time.

## Alternatives considered

- *Trusting portal URIs:* exfiltration risk.
- *A PR gate on every portal change:* rejected by the user (D4).
