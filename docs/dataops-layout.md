# Pipelines in the DataOps layout

For teams whose Airflow DAGs are JSON files deployed by the DataOps platform (ADR-0026). The spec is the same as in
the mwaa layout; only what `factory build` writes differs.

## 1. Set up the repository (once)

New repository:

```bash
factory init my-repo --layout dataops --team my_team --project rd-dpl-studydata \
  --dist-bucket <deployment bucket> --base-image <ECR image of the base OS> \
  --ca-bundle /etc/pki/tls/certs/ca-bundle.crt --proxy http://<proxy>:<port> --no-proxy ".corp.example,.amazonaws.com"
```

Existing repository: run the same command inside it. Existing files are kept. The output lists what to add to
`pyproject.toml` (the library's dependencies, the lint/type/coverage exclusions) and to Sonar; then rebuild the Docker
image once.

Fill in `platform-settings/<env>.json` per environment:
- `region` and the team's `account_id`;
- `dataops.state_bucket`: run state, staging, audit and bundles, under `dataops.state_prefix`;
- `dataops.vault.address`: only if pipelines read secrets.

Environments default to `dev, sit, ppt, prd`.

## 2. Create a pipeline

```bash
factory new s3_copy --starter s3_to_s3_event     # binding blocks for every environment of the repository
# fill pipelines/s3_copy/spec/*.yaml (factory validate s3_copy lists what is left)
factory validate s3_copy
factory lock s3_copy
factory build s3_copy
factory verify s3_copy
```

`factory build` writes:

| File | What it is |
|---|---|
| `dags/s3-copy.json` | The DAG for DataOps: one container task running `s3_copy/task.py` |
| `src/s3_copy/task.py` | The Airflow task: one function per step, documented with the spec's plain English |
| `src/s3_copy/config_<env>.json` | Region, state bucket, queue URL, Vault location per environment (no secrets) |
| `src/s3_copy/pipeline_bundle.zip` | The locked spec the task runs; task.py pins its sha256 |
| `src/shared/factory/` | The framework library (vendored; shared by every generated pipeline) |
| `tests/s3_copy/test_task.py` | Structure checks and a smoke run against moto |
| `docs/pipelines/s3_copy.md`, `docs/config-schemas/s3_copy.schema.json` | Pipeline documentation, config schema |
| `pipelines/s3_copy/BUILD-MANIFEST.json` | What was generated (`factory verify` compares against it) |

Never edit generated files: change the spec and rebuild. `factory build` refuses to write over files it did not
generate, so hand-written pipelines are never touched.

## 3. Check and run locally

```bash
uv sync --dev
uv run ruff check src/ tests/ && uv run mypy src/
PYTHONPATH=src uv run pytest tests/s3_copy/ -v
project_env=dev FACTORY_WORKDIR=/tmp/factory PYTHONPATH=src uv run python src/s3_copy/task.py   # real dev AWS
```

## 4. How a run works

- **Queue triggers** (`s3_event`, `sqs`): every DAG run polls the queue (`*/5 * * * *` unless `schedule_cron` is
  set). Each message is checked and becomes one run, keyed by the spec's run key.
  - A message is deleted when its run finished.
  - A failed run leaves the message for redelivery. The queue's dead-letter queue catches messages that keep
    failing.
  - Duplicates are acknowledged without rerunning. Unreadable messages are quarantined.
- **Schedule**: one run per DAG run, keyed by its start minute.
- **Manual**: the conf, with `run_key`, comes from `FACTORY_RUN_CONF`.
- A rerun with the same run key skips finished steps and never writes a delivered file twice.
- **Exit codes:** 0 ok, 65 data, 70 internal, 75 transient, 77 security, 78 configuration.
- **Secrets**: `secrets: {default: dataops_vault}` in `environments.yaml` reads them from the project's Vault (token
  from Secrets Manager); only names appear in specs, configs and logs.

## 5. Deploy

Deployment follows the DataOps process described in `docs/DEPLOYMENT.md`:
1. Sync the branch.
2. Rebuild the image only if `docker/` or the dependencies changed.
3. Deploy `dags/<dag>.json`.

## Not supported in this layout (yet)

- `fivetran.sync` (Fivetran pipelines are deployed with git and the connector SDK).
- Resources from the resource catalog.
- Per-environment schedules.
- A proxy for HTTP ops.
