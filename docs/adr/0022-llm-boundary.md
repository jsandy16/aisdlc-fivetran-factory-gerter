# ADR-0022: No real data reaches an LLM; configurable model provider

- **Status:** Accepted
- **Date:** 2026-10-13

## Context

Study and customer data must never be sent to an LLM. Confidentiality requirements may require AWS-hosted model access.

## Decision

- **What agents see:** specs, schemas, the op catalog, synthetic fixtures and validator output only, with requirement text redacted and fenced.
- **What agents lack:** file, network and shell tools, and data-plane credentials.
- **Provider:** configurable: Anthropic API, Amazon Bedrock or Claude Platform on AWS.
- **Replay keys** use a logical model id; the provider model id is kept in provenance.
- **Default model** is the latest pinned Claude model id, changed deliberately with re-recorded fixtures.

## Consequences

- Confidential data cannot leak through prompts by construction.
- The hosting choice follows corporate policy.

## Alternatives considered

- *Letting agents inspect real samples:* rejected.
