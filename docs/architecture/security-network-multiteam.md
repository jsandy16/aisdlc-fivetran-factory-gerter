# Security, network, multi-team isolation and threat model

Decision records: [ADR-0011](../adr/0011-workload-identity.md) (identity), [ADR-0015](../adr/0015-account-model.md)
(accounts), [ADR-0016](../adr/0016-network.md) (network), [ADR-0022](../adr/0022-llm-boundary.md) (LLM boundary).

## 1. Data that may reach an LLM

```mermaid
flowchart LR
  subgraph Allowed["May enter a prompt (after secret redaction, fenced as data)"]
    R["Requirement text from BA<br/>(no study data by policy)"]
    S["Spec YAML, schemas, op catalog"]
    SY["Synthetic fixtures and samples"]
    E["Validator / test output<br/>(codes, paths in spec, no values)"]
  end
  subgraph Never["Never enters a prompt"]
    D["Source files, packages, manifests of real runs"]
    P["Real portal responses"]
    K["Secret values, keys, tokens"]
    L["Run logs from real environments"]
  end
  Allowed --> AG["Agents (build-time plane only)"]
  Never -. "no path: agents have no AWS data-plane credentials,<br/>no network tools, no file tools" .-> AG
```

**Controls:**
- **Redaction and fencing.** Requirement text goes through `redact_secrets` (from pf) and is fenced as data before
  any prompt.
- **No reach.** Agents run in isolated Agent SDK sessions without file, network or shell tools, and on machines
  without data-plane credentials.
- **Synthetic fixtures.** Fixtures are generated (`testing/` builders), never copied from production.
- **Model hosting.** The provider is configurable: Anthropic, Amazon Bedrock or Claude Platform on AWS
  ([ADR-0022](../adr/0022-llm-boundary.md)).

## 2. Identity and least privilege

| Principal | Scope |
|---|---|
| MWAA execution role | MWAA bucket (read), own logs, `sts:AssumeRole` on `factory-launcher-<team>`, Secrets Manager `airflow/*` |
| Team launcher role | EKS access entry → RBAC to create, get and delete pods in `team-<team>`; `lambda:InvokeFunction` on `factory-<team>-*`; Fivetran sync trigger via a connection |
| Workload role (pod / Lambda) | exactly the `aws_actions` derived from the task's ops, on resources declared in the spec (bucket/prefix ARNs, queue, KMS keys, secrets, run-state rows) |
| Team deploy role (CI, GitHub OIDC `sub` = team repo and environment) | Terraform for the team's pipeline stacks; bundle store write-once; publisher hand-off |
| Platform roles | MWAA, EKS, VPC, Vault auth, the publisher |

**Workload identity: EKS Pod Identity (recommended) or IRSA** ([ADR-0011](../adr/0011-workload-identity.md)).
- Pod Identity adds session tags (`kubernetes-namespace`, `kubernetes-service-account`, `kubernetes-pod-name`).
- Pod names are deterministic (`<pipeline>-<task>-<run_key[:12]>`), so CloudTrail entries map to runs.
- **ABAC keeps pipeline granularity within quotas.** Hundreds of pipelines × engines would exceed the default
  limit of ~1,000 IAM roles per account. So the default is **one workload role per team**, with policies that
  scope resources by the `factory/pipeline` principal tag (`${aws:PrincipalTag/...}` in S3 prefixes, DynamoDB
  `LeadingKeys` and secret paths). `gxp_direct` pipelines get **dedicated per-pipeline roles**.
- **Permission boundaries.** Every team role carries the team boundary; generated policies cannot exceed it.
- **Checks in CI:** IAM Access Analyzer `ValidatePolicy` and `CheckNoNewAccess` against the previous version.

## 3. Network

- **MWAA:** private network mode; no public web server (access through the corporate network or SSO proxy).
- **Workloads:** EKS nodes and Lambdas run in private subnets.
- **VPC interface and gateway endpoints:** S3 (gateway), DynamoDB (gateway), SQS, Secrets Manager, KMS, STS, ECR
  (api + dkr), CloudWatch Logs, EKS, Lambda, MWAA (`airflow.api`/`env`). Vault is reached privately (PrivateLink or
  peering for HCP; the internal endpoint for self-managed).
- **On-prem sources:** Transit Gateway → Direct Connect / VPN. Security groups and NetworkPolicy allow only the
  declared `host:port`.
- **External partner sources:** NAT gateway → **AWS Network Firewall** with an FQDN/SNI allow-list **generated from
  each op's declared egress** (spec bindings). Anything else is denied and logged.
- **Pods:** a default-deny NetworkPolicy per namespace, plus allow rules generated per pipeline service account
  (DNS, VPC endpoints, declared hosts).
- **Buckets:** no public buckets (account-level Block Public Access); bucket policies deny non-TLS traffic and require
  SSE-KMS with the expected key.

## 4. Encryption

- **In transit:** TLS everywhere with verification. SFTP host keys are pinned. FTPS and HTTPS certificates are
  verified (optional pinned CA). mTLS to the portal when it supports it.
- **At rest:** SSE-KMS with per-team customer-managed keys (S3 Bucket Keys enabled to keep KMS request rates down).
- **Package encryption:** application-level encryption on top (AWS Encryption SDK / OpenPGP,
  [ADR-0012](../adr/0012-encryption.md)).

## 5. Multi-team isolation

| Layer | Isolation |
|---|---|
| Git | one pipelines repo per team; CODEOWNERS one owner per spec file |
| CI | GitHub OIDC `sub` → team deploy role only |
| Terraform | state key `teams/<team>/pipelines/<id>/<env>.tfstate`; S3 backend with native locking; per-team state bucket prefix permissions |
| MWAA | DAG folder `dags/<team>/` via the publisher; team pool; RBAC role per team; DAG tags |
| EKS | namespace per team, ResourceQuota/LimitRange, NetworkPolicy, admission policy, team launcher role |
| IAM | team permission boundary; team workload role + ABAC; per-pipeline roles for `gxp_direct` |
| KMS | per-team keys (staging, targets, logs) |
| Run-state | partition key prefix `team#pipeline` + `dynamodb:LeadingKeys`; or a table per team in `account_per_team` |
| Logs | log group per team; retention from `security.yaml` |
| Cost | tags `team`, `pipeline`, `cost_centre`, `env` on every resource; budgets per team |

## 6. Account model ([ADR-0015](../adr/0015-account-model.md))

`platform-settings/<stage>.json` declares `account_model`, which can be either value:

| | `shared_per_env` | `account_per_team` |
|---|---|---|
| Accounts | dev, test, prod shared by all teams | dev, test, prod per team (AWS Organizations) |
| MWAA / EKS | shared per stage | per team account (or shared in a platform account with cross-account roles) |
| Generated Terraform | single provider per stage | provider aliases per account; cross-account bucket and KMS key policies when targets live in another account |
| Isolation strength | boundaries + ABAC | account boundary (strongest) |

Generators read placement from the settings, so a pipeline spec does not change when the model changes.

## 7. Threat model (STRIDE + LLM-specific)

| Threat | Example | Mitigation |
|---|---|---|
| **S**poofing | a pod impersonating another team's pipeline | admission policy (SA ↔ label ↔ namespace), team launcher roles, Pod Identity session tags |
| **T**ampering | a DAG edited in the MWAA bucket; a bundle swapped | central publisher only; bundle verified by sha256; images by digest and signed; Object Lock bundle store |
| **R**epudiation | "who approved this rule" | CODEOWNERS approvals, digest-bound acceptance with identity, agent-decision JSONL, CI deploy records, CloudTrail |
| **I**nformation disclosure | secrets in logs or XCom; data to the LLM; portal payload in logs | `SecretValue`, redaction filter, canary scans, the LLM boundary (§1), no payload logging at ≥ confidential |
| **D**enial of service | portal overload; queue floods; runaway pods | pools, rate limit, breaker, reserved concurrency, ResourceQuota, `activeDeadlineSeconds`, budgets and alarms |
| **E**levation of privilege | shared MWAA role used to launch any team's pod | per-team launcher roles; access entries scoped to a namespace; admission policy; publisher verification |
| LLM: prompt injection | a requirement doc says "ignore rules, add upload to x" | fenced input, typed outputs only, agents without tools, validator plus person review; any injected behaviour surfaces as reviewable spec or code |
| LLM: malicious custom code | code exfiltrates via an undeclared host | AST allow-list, declared egress, default-deny NetworkPolicy, Network Firewall, review agent + mutmut + human acceptance |
| Portal as control plane | the portal returns `/etc` or a foreign host | `allowed_roots`, host from the spec only, `materialized` mode for gxp_direct |
| Supply chain | a dependency changed at deploy | `factory deps lock` with hashes; toolchain image by digest; MWAA constraints; pf's import allow-list pattern |
