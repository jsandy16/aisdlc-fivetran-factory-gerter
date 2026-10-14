# Developing a pipeline locally

1. **Secrets** (dev only). Run `factory secrets template <pipeline>`. It writes
   `~/.factory/secrets/<team>/dev.yaml` (mode 0600) with every secret name and key the pipeline needs, values
   empty. Fill the values in yourself. Never commit or paste the file; agents cannot read it.
2. **Run it.** Run `factory run <pipeline> --message msg.json` (message triggers) or `--conf conf.json` (manual and
   schedule). Every task of the generated DAG runs on your machine, with the settings the deployed task would get:
   - `--stores local` (default): the framework's staging, bundles, audit and run state on a local S3 and DynamoDB
     endpoint. `--stores aws` uses the dev stores.
   - `--data aws` (default): the real dev buckets the bindings name, reached with your AWS credentials, and the real
     portal, APIs and partner servers. `--data local --seed BUCKET=FOLDER` uses local buckets seeded from folders.
   - `--task T` runs one task; `--skip T` skips a task you cannot run here (for example a Fivetran sync without a
     key). The report says per task: ran locally, remote endpoints called, or skipped. The log is written to
     `.factory/runs/`.
3. **Infrastructure.** By default the framework's Terraform creates the pipeline's roles, queues, log groups, compute
   and alerts. If the infra team provides them, set `infrastructure` in `environments.yaml`:
   - `mode: existing` for a whole environment, or per group;
   - `names` where their names differ from the framework's.

   Then hand them `build/<pipeline>/infra/<env>.md`, and check their work with `factory infra check <pipeline>`.
4. **More AWS services.** Declare them in `environments.yaml` → `resources`, using a type from the resource catalog
   (`sns_topic`, `cloudwatch_metric_alarm`, `cloudwatch_dashboard`, `eventbridge_rule`, `s3_bucket`, `sqs_queue`,
   `dynamodb_table`, …). A rule can notify a declared topic with the `notify.publish` op.

Local runs are never allowed in ppt or prod; the runtime refuses a local secrets file there.
