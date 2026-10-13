# ADR-0016: Private-by-default networking; egress allow-lists generated from the destination and source allow-lists

- **Status:** Accepted
- **Date:** 2026-10-13
- **Amended:** egress generated from the destination allow-list (D2, ADR-0024).

## Context

Sources and destinations include S3, on-prem servers (DX/VPN through a Transit Gateway) and external partners (SFTP/HTTPS). Data is highly confidential.

## Decision

- **Private placement:** MWAA in private mode (platform account). Lambdas and EKS in private subnets of the team account.
- **VPC endpoints:** S3, DynamoDB, SQS, Secrets Manager, KMS, STS, ECR, Logs, EKS, Lambda.
- **External destinations** go through NAT + AWS Network Firewall, with an FQDN/SNI allow-list **generated from `destinations.yaml`** (sftp/https entries) and from declared source hosts. **On-prem** traffic goes through the Transit Gateway.
- **Pods:** a default-deny NetworkPolicy per namespace.
- **Buckets:** Block Public Access; TLS-only and SSE-KMS bucket policies.

## Consequences

- The portal cannot cause egress to a host that is not in the reviewed allow-list.

## Alternatives considered

- *Open egress with proxy logging:* gives no prevention.
