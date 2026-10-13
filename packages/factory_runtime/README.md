# factory_runtime (Phase 3)

Runs inside EKS pods and Lambda. Contents:
- the built-in op library: portal, discover, transfer, package, crypto, checksum, s3, sqs, secret, http, naming;
- streaming sources: sftp, ftps, https, s3;
- the SecretProvider (Vault and Secrets Manager);
- the portal client, run-state, manifest and the custom-rule plugin API.

See [op-catalog](../../docs/architecture/op-catalog.md) and [execution-model](../../docs/architecture/execution-model.md).
