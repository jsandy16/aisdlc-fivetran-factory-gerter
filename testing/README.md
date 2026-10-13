# testing/

The test stand-ins live in the dev-only workspace package
[`packages/factory_testing/`](../packages/factory_testing/) so tests can import them; it is never a runtime
dependency. It provides moto (S3, SQS, DynamoDB, Secrets Manager), a fake Vault, a mock portal, a mock REST API, a
mock Fivetran API, SFTP and HTTPS servers, seeded test keys and certificates, and the end-to-end harness. All data
is synthetic. The Airflow DAG-integrity harness arrives in Phase 5.
