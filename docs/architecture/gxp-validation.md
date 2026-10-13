# GxP, 21 CFR Part 11 and ALCOA+ approach

Decision record: [ADR-0021](../adr/0021-gxp-controls.md).

This is a design intent document, not a validation plan. QA owns the validation approach (open question Q4).

## 1. ALCOA+ mapping

| Principle | How the framework supports it |
|---|---|
| **Attributable** | Specs and rule changes go through PRs with CODEOWNERS (one owner per file). Custom-rule acceptance records a person's identity and a content digest. Agent decisions record the driving person. Deploys are tied to CI runs (OIDC). Run actions are tied to pod/Lambda identities (Pod Identity session tags → CloudTrail). |
| **Legible** | YAML specs, generated docs and plain-English rules; JSON logs with a fixed schema. |
| **Contemporaneous** | Audit events are written at the time of the action (secret access, portal call, rule result, upload). |
| **Original** | Packages and manifests are written once to targets (`If-None-Match`); originals and evidence go to the Object Lock audit store. |
| **Accurate** | Checksums computed while streaming plus an independent re-read check; manifest match validation; decrypt-verify option for `gxp_direct`. |
| **Complete** | Every delivery has a manifest (every file: path, size, sha256), and every run has an audit trail. Rejected or quarantined items are recorded, never dropped silently. |
| **Consistent** | Deterministic generation; `logic_sha` identical across environments; run pinning. |
| **Enduring** | Object Lock (compliance mode) for audit and evidence, with retention from `security.yaml`; specs and history in git. |
| **Available** | Docs, review packages and evidence can be retrieved by pipeline, run_key and version. |

## 2. Part 11 considerations

- **Electronic records:** specs, locks, review packages, manifests and audit events, all immutable once written
  (git history and Object Lock).
- **Electronic signatures:** the proposed signature is a PR approval by an SSO-backed GitHub identity, plus a
  digest-bound acceptance record. Whether QA accepts this or needs an eQMS signature is open question Q4.
- **Audit trails:** CloudTrail, run audit events, agent decisions and git history. Generated docs link them per
  version.
- **Access control:** per-team isolation ([security doc](security-network-multiteam.md)); person-only actions are
  refused in agent sessions.

## 3. Validation strategy (proposal)

- **Validate the framework once (risk-based, CSA-style):**
  - the op library;
  - the generators;
  - the gates and the change engine,
  
  each with its test suites as objective evidence.
- **Per pipeline, evidence is generated automatically.** The traceability matrix requirement id → rule →
  implementation → test case → result comes from the spec and the test reports and is included in the review
  package.
- **Framework upgrades:** run a regression over a **golden corpus** of pipelines. Because there are no per-file
  version stamps, unchanged pipelines produce zero diff, and only changed outputs need re-review.

## 4. GxP-specific spec rules (validator)

| gxp_tier | Requirements |
|---|---|
| `non_gxp` | defaults |
| `gxp_indirect` | encryption on every target; sha256 in the manifest; audit store; no spot; every custom rule accepted by a person; break-and-verify coverage of every enforced validation |
| `gxp_direct` | all of the above, plus dedicated per-pipeline roles; portal `materialized` mode (recommended); decrypt-verify step; an optional dedicated MWAA environment; uncovered checks are errors |
