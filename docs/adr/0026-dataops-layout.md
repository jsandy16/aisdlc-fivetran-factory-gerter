# ADR-0026: The dataops layout: DataOps DAG JSON, readable task.py, the team repository

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

The organisation's Airflow pipelines are not Python DAGs on a team MWAA environment (the mwaa layout). They live in
team repositories with a fixed structure and are deployed by the DataOps platform:
- `dags/<name>.json` describes a DAG (`name`, `schedule`, `version`, `tasks{entrypoint, envs, docker}`).
- Each task runs as an ECS container. Its `entrypoint.py` syncs `src/` from the deployment bucket, then runs
  `python <pipeline>/task.py`.
- `src/<pipeline>/config_<env>.json` is selected by `project_env`.
- Secrets come from the platform's Vault through a project token held in Secrets Manager.
- Queue-triggered pipelines poll their queue on a schedule.

Fivetran pipelines are deployed separately (git and the connector SDK) and are out of scope.

## Decision

- **A repository layout switch.** `factory.toml` gets `layout = "dataops"` and a `[dataops]` table (project,
  deployment bucket, base image, Python, proxy, CA bundle). Platform settings get a `dataops` section per environment:
  - the state bucket and prefix;
  - the DataOps Vault (address, token secret, KV path);
  - optionally the project entity.

  `mwaa` and team `stores` become optional there. The specs, validation, locks, change plans and the op catalog are
  unchanged.
- **`factory init --layout dataops`** writes the skeleton once:
  - `pyproject.toml`, `docker/Dockerfile`, `docker/workdir/entrypoint.py`;
  - CI, pre-commit, Sonar settings;
  - `README.md`, `AGENTS.md`, architecture and deployment docs;
  - platform settings templates.

  In an existing repository it keeps every existing file and lists what to add (dependencies and the exclusions for
  the vendored library). `factory new` fits a starter to the repository's environments.
- **`factory build` writes into the repository** (no `build/` folder):
  - `dags/<dag>.json`: one container task; the schedule comes from the trigger, or `*/5 * * * *` for queue polling;
  - `src/<pipeline>/task.py`: one function per step, with the step's plain English as its docstring, one function
    per task, and `main()`;
  - `src/<pipeline>/config_<env>.json`;
  - `src/<pipeline>/pipeline_bundle.zip`: the locked spec; its sha256 is pinned in task.py;
  - `tests/<pipeline>/test_task.py`;
  - `docs/pipelines/<pipeline>.md` and `docs/config-schemas/<pipeline>.schema.json`;
  - the framework library, vendored in `src/shared/factory/`.

  `pipelines/<pipeline>/BUILD-MANIFEST.json` records every generated file. `factory verify` reports edited, missing,
  stale and unexpected files. A build never writes over a file no manifest recorded, so hand-written pipelines stay
  untouched.
- **Vendored library.** `src/` is what the containers sync, so a framework upgrade is a rebuild. A Docker image
  rebuild is needed only when its dependencies change. Lint, type checks, coverage and Sonar exclude the folder.
- **Container runtime** (`factory_runtime.container`). task.py calls
  `Pipeline.load(...).run_each(run_pipeline)`; each step calls `task.step(rule_id)`. The executor became a
  `TaskSession` that runs a task rule by rule. Checkpoints, failure classes, audit and secrets are as before.
  Steps out of the spec's order are refused.
- **Starting runs in the container.**
  - Queue triggers poll inside the container, reusing the dispatcher's checks: envelope, fields, run key, write-once
    custody snapshot, duplicates and quarantine.
  - A message is deleted when its run finished. A failed run leaves the message for redelivery, so the queue's
    dead-letter queue bounds the attempts.
  - Scheduled runs are keyed by their start minute. Manual runs read their conf from `FACTORY_RUN_CONF`.
  - The exit code is the most serious failure class.
- **S3-only state.** Run state, staging, audit and published bundles are objects under the environment's state
  prefix. Run state uses S3 conditional writes (`If-None-Match`, `If-Match`) with the same semantics as the DynamoDB
  store. No new infrastructure is needed.
- **DataOps Vault provider** (`dataops_vault`). The token comes from Secrets Manager
  (`/{service_env}/pcp/{entity}/vault-token`) and secrets from KV v2 (`secret/data/{entity_path}/base/{name}`).
  The entity comes from settings or, as in DataOps containers, from `AWS_ROLE_ARN`. Values stay in `SecretValue`.
- **Strict environments.** `prd` joins `ppt` and `prod`. The image's proxy and CA variables are accepted in strict
  environments only with the exact values declared in `factory.toml`, and the declared CA bundle is trusted for
  HTTPS.

## Consequences

- One spec can target either layout. Lambda/EKS engine choices and the Terraform stack do not apply in the dataops
  layout: every task runs in the pipeline's container.
- The team's existing CI (ruff, mypy, pytest with coverage) runs on generated pipelines unchanged.
- **Not done here:**
  - deployment calls to the DataOps service;
  - proxy support for HTTP ops (they connect directly);
  - per-environment schedules (one DAG file serves all environments);
  - resources from the resource catalog;
  - `fivetran.sync` (refused by the validator).
