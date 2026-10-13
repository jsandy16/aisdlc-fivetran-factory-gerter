# packages/

The Pipeline Factory v2 packages, managed as a [uv](https://docs.astral.sh/uv/) workspace with a virtual root
(`pyproject.toml` here, `uv.lock` committed). pf 0.1.0 at the repository root is not a member: new code adapts pf
code and never imports it (ruff `banned-api`). See [repo-structure](../docs/architecture/repo-structure.md) and the
[Phase 2 design](../docs/architecture/phase-2-design.md).

| Package | Phase | What |
|---|---|---|
| [factory_core](factory_core/) | 2 | errors and exit codes, canonical JSON hashing, result envelope, units, YAML, CEL, templates, destination URIs, path patterns |
| [factory_spec](factory_spec/) | 2 | spec v1 models, op catalog, loader, validators, lock, JSON Schema export, migrations, patch engine, BA view |
| [factory](factory/) | 2 (more in 3–6) | the `factory` CLI and the starter templates |
| [factory_runtime](factory_runtime/) | 3 | pod/Lambda runtime (placeholder) |
| [factory_airflow](factory_airflow/) | 3 | MWAA operators and callbacks (placeholder) |

```bash
cd packages
pip install uv==0.11.32 && uv sync --locked --all-packages
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
uv run factory schema --check                         # committed JSON Schemas match the models

cd ../examples/team-pipelines                         # the worked example's team repository
uv run --project ../../packages factory validate study_pkg_delivery
uv run --project ../../packages factory lock study_pkg_delivery --check
uv run --project ../../packages factory show study_pkg_delivery --view ba
```

Tests use synthetic data only (made-up accounts, hosts, studies and owners).
