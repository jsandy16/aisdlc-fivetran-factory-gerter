# starters/

The starter templates ship inside the `factory` package so they work from an installed wheel:
[`packages/factory/src/factory/starters/`](../packages/factory/src/factory/starters/).

| Starter | What it builds | Engine |
|---|---|---|
| `s3_package_deliver` | The worked example's shape: SQS message names an S3 package → zip with manifest → checksum → deliver to portal-resolved destinations x/y (allow-listed) | Lambda (auto) |
| `s3_to_s3_event` | S3 event → server-side copy to a static archive bucket | Lambda |
| `rest_to_s3` | Daily REST API export (cursor pagination) → zip + manifest → static S3 landing bucket | auto |
| `table_to_s3_fivetran` | Database tables → Fivetran native connector → lake (model only; provisioning is Phase 5) | `fivetran_native` |

```bash
factory new my_pipeline --starter s3_package_deliver
factory validate my_pipeline --allow-unfilled     # lists every <REQUIRED> value still to fill, by file
```

Application-level package encryption is deferred (ADR-0012), so the Phase 1 name `portal_zip_encrypt_s3` became
`s3_package_deliver`. Each starter has a test fill file (`packages/factory/tests/starter_fills/`) proving it
validates cleanly once filled.
