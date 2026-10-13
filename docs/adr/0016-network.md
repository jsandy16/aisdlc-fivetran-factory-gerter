# ADR-0016: Private-by-default networking with egress allow-lists generated from declared op egress

- **Status:** Proposed (Phase 1 review)
- **Date:** 2026-10-13

## Context

Sources are both on-prem (DX/VPN through a Transit Gateway) and external partners. Data is highly confidential.

## Decision

- **Private placement:** MWAA in private mode; EKS and Lambda in private subnets.
- **VPC endpoints:** S3, DynamoDB, SQS, Secrets Manager, KMS, STS, ECR, Logs, EKS, Lambda, MWAA.
- **On-prem** sources go through the Transit Gateway.
- **External** sources go through NAT + AWS Network Firewall with an FQDN/SNI allow-list generated from op `egress` declarations.
- **Pods:** a default-deny NetworkPolicy per namespace, with per-pipeline allow rules.
- **Buckets:** account-level Block Public Access; TLS-only and SSE-KMS bucket policies.

## Consequences

- Undeclared egress is blocked and logged.
- New hosts need a spec change, which is reviewed.

## Alternatives considered

- *Open egress with proxy logging:* rejected, because it gives no prevention.
