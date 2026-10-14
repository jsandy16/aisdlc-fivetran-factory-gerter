"""`factory init --layout dataops`: the repository skeleton, modelled on the DataOps team repositories (ADR-0026).

Written once and then owned by the team (unlike the per-pipeline files, which `factory build` regenerates). In an
existing repository nothing is overwritten: files that exist are kept, and `init` lists what to add to them (the
library's dependencies, the lint/type/coverage exclusions for the vendored library).
"""

from __future__ import annotations

import re
from importlib import metadata
from typing import Any

from factory_spec.repo import DataopsRepo

VENDOR_GLOB = "src/shared/factory"


def runtime_dependencies() -> list[str]:
    """What the vendored library needs at run time, from the installed framework's own metadata."""
    out: dict[str, str] = {}

    def walk(dist: str, extras: frozenset[str]) -> None:
        for req in metadata.requires(dist) or []:
            spec, _, marker = req.partition(";")
            m = re.search(r'extra\s*==\s*"([^"]+)"', marker)
            if m and m.group(1) not in extras:
                continue
            spec = spec.strip()
            name = re.split(r"[\[<>=!~ ]", spec, maxsplit=1)[0]
            wanted = frozenset(x.strip() for x in re.findall(r"\[([^\]]*)\]", spec)[0].split(",")) if "[" in spec \
                else frozenset()  # fmt: skip
            if name.startswith("factory-") or name.startswith("factory_"):
                walk(name, wanted)
            else:
                out.setdefault(name.lower(), re.sub(r"\[[^\]]*\]", "", spec))

    walk("factory-runtime", frozenset())
    return [out[k] for k in sorted(out)]


def _toml_list(items: list[str], indent: str = "    ") -> str:
    return "\n".join(f'{indent}"{i}",' for i in items)


def pyproject(ds: DataopsRepo) -> str:
    deps = sorted({*runtime_dependencies(), "pandas", "requests", "PyYAML", "pip_system_certs"}, key=str.lower)
    return f"""[project]
name = "dops-{ds.project}"
version = "0.1.0"
requires-python = ">={ds.python.removeprefix("python")}"
dependencies = [
{_toml_list(deps)}
]

[dependency-groups]
dev = [
    "pytest",
    "pytest-cov",
    "ruff",
    "pre-commit",
    "mypy",
    "moto[s3,sqs,secretsmanager]",
]

[tool.ruff]
line-length = 100
extend-exclude = ["{VENDOR_GLOB}"]  # the vendored Pipeline Factory library (generated)

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["E501"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"

[tool.mypy]
python_version = "{ds.python.removeprefix("python")}"
ignore_missing_imports = true
check_untyped_defs = true
exclude = ["tests/", "{VENDOR_GLOB}/"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.coverage.run]
omit = ["{VENDOR_GLOB}/*"]
"""


def dockerfile(ds: DataopsRepo) -> str:
    lines = [
        f"FROM {ds.base_image}",
        "",
        f"RUN dnf install -y zip openssl {ds.python} {ds.python}-pip ca-certificates && dnf clean all",
        "",
        f"RUN pip{ds.python.removeprefix('python')} install uv=={ds.uv_version}",
        "",
        "WORKDIR /workdir",
        "COPY pyproject.toml uv.lock ./",
        "RUN uv sync --no-dev --frozen --no-install-project --no-cache && rm pyproject.toml uv.lock",
        "",
    ]
    if ds.ca_bundle:
        lines += ["# Corporate CA trust: put the CA certificates in docker/certs/",
                  "COPY docker/certs/ /etc/pki/ca-trust/source/anchors/", "RUN update-ca-trust extract", "",
                  f"ENV REQUESTS_CA_BUNDLE={ds.ca_bundle}", f"ENV SSL_CERT_FILE={ds.ca_bundle}", ""]  # fmt: skip
    lines += [
        "COPY docker/workdir/ .",
        "RUN chmod -R +x /workdir/",
        'ENV PATH="/workdir/.venv/bin:$PATH"',
        "",
        'RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && unzip awscliv2.zip '
        "&& ./aws/install && rm -rf awscli*",
        "",
    ]
    if ds.proxy:
        lines += [f'ENV {k}="{ds.proxy}"' for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")]
    if ds.no_proxy:
        lines += [f'ENV {k}="{ds.no_proxy}"' for k in ("NO_PROXY", "no_proxy")]
    lines += ["", 'ENTRYPOINT ["/workdir/entrypoint.py"]', ""]
    return "\n".join(lines)


def entrypoint(ds: DataopsRepo) -> str:
    return f'''#!/usr/bin/{ds.python}

import os
import subprocess
import sys

print("-------------------- START OF AIRFLOW TASK NODE --------------------", flush=True)
print("Project repo: {ds.project}", flush=True)

_s3_pull_branch = os.environ.get("S3_PULL_BRANCH", os.environ.get("{ds.env_var}"))
subprocess.run(
    [
        "aws", "s3", "sync", f"s3://{ds.dist_bucket}/deployments/{{_s3_pull_branch}}", ".",
        "--exclude", "*", "--include", "src/*",
    ],
    check=True,
)

os.chdir(f"{{os.getcwd()}}/src/")

p = subprocess.run(["{ds.python}"] + sys.argv[1:])
print("--------------------- END OF AIRFLOW TASK NODE ---------------------", flush=True)
if p.returncode != 0:
    raise Exception(f"Invalid result while executing {{sys.argv[1]}}: {{p.returncode}}")
'''


CI = """name: CI

on:
  push:
    branches: [main, "feature/**", "fix/**"]
  pull_request:
    branches: [main]
  workflow_dispatch:

jobs:
  quality-gate:
    name: Quality Gate
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5
        with:
          python-version: "3.13"
      - name: Install uv
        run: pip install uv
      - name: Install dependencies
        run: uv sync --dev --frozen --no-install-project
      - name: Lint
        run: uv run ruff check src/ tests/
      - name: Format check
        run: uv run ruff format --check src/ tests/
      - name: Type check
        run: uv run mypy src/
      - name: Tests with coverage
        run: uv run pytest --cov=src --cov-report=xml --cov-report=term --cov-fail-under=70
"""

PRE_COMMIT = """repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.15.20
    hooks:
      - id: ruff-check
        args: [--fix]
      - id: ruff-format
"""


def sonar(ds: DataopsRepo) -> str:
    return f"""sonar.projectKey=dops-{ds.project}-repo
sonar.projectName=dops-{ds.project}-repo
sonar.sources=src
sonar.tests=tests
sonar.language=py
sonar.python.version={ds.python.removeprefix("python")}
sonar.sourceEncoding=UTF-8
sonar.python.coverage.reportPaths=coverage.xml
sonar.exclusions=**/__pycache__/**,**/*.pyc,**/.venv/**,{VENDOR_GLOB}/**
sonar.coverage.exclusions={VENDOR_GLOB}/**
"""


def readme(ds: DataopsRepo, team: str) -> str:
    return f"""# dops-{ds.project}-repo

Data pipelines of team `{team}` on the DataOps MWAA platform. Each pipeline runs as a Docker container started by an
Airflow DAG defined in `dags/`.

Pipelines are described as specs in `pipelines/<pipeline>/spec/` and generated with Pipeline Factory:

```bash
factory new <pipeline> --starter <starter>    # a new spec
factory validate <pipeline>                     # check it
factory lock <pipeline> && factory build <pipeline> && factory verify <pipeline>
```

`factory build` writes `dags/<dag>.json`, `src/<pipeline>/` (task.py, config_<env>.json, the pinned spec bundle),
`tests/<pipeline>/` and `docs/pipelines/<pipeline>.md`, plus the framework library in `src/shared/factory/`. Do not
edit generated files: change the spec and rebuild (`factory verify` reports edits).

## Local setup

```bash
pip install uv
uv sync --dev
uv run pre-commit install
PYTHONPATH=src uv run pytest tests/ -v
```

See [AGENTS.md](AGENTS.md) for conventions and [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for deployment.
"""


def agents(ds: DataopsRepo) -> str:
    return f"""# AGENTS.md: project context for AI coding assistants

## Layout

```
pipelines/<pipeline>/spec/   ← the pipeline spec (source of truth for generated pipelines)
src/<pipeline>/task.py       ← pipeline entry point (always task.py)
src/shared/                  ← shared utilities, never pipeline-specific logic
src/shared/factory/          ← the Pipeline Factory library (generated, vendored: never edit)
tests/<pipeline>/            ← mirrors src/
dags/                        ← Airflow DAG definitions (JSON)
docker/                      ← Dockerfile and entrypoint.py
```

## Generated pipelines

A pipeline with `pipelines/<pipeline>/BUILD-MANIFEST.json` is generated by Pipeline Factory. Its `task.py`,
`config_<env>.json`, `pipeline_bundle.zip`, DAG JSON, tests and docs are regenerated by `factory build`. Change the
spec (`pipelines/<pipeline>/spec/*.yaml`) instead of these files; `factory verify` reports hand edits. Hand-written
pipelines (without a manifest) follow the conventions below and are never touched by the factory.

## Conventions

- `{ds.env_var}` selects `config_<env>.json`; never hard-code credentials, ARNs or account ids in code.
- Secrets are references (names) only, read at run time; never logged.
- Logging via `logging.getLogger(__name__)`, format `"%(asctime)s | %(levelname)s | %(name)s | %(message)s"`.
- ruff (E, F, I, UP, B), line length 100; mypy on `src/`; tests with `PYTHONPATH=src uv run pytest`.
- Use synthetic data in tests and examples; real data never goes into code, tests or prompts.
"""


def architecture(ds: DataopsRepo) -> str:
    return f"""# Architecture

Each pipeline runs as a Docker container started by an Airflow DAG on the DataOps MWAA platform.

```
MWAA DAG (dags/<dag>.json)
    └── ECS task (docker/Dockerfile)
            ├── entrypoint.py: aws s3 sync s3://{ds.dist_bucket}/deployments/<branch> (src/ only)
            └── {ds.python} <pipeline>/task.py
```

Generated pipelines (Pipeline Factory) run their steps through the vendored library in `src/shared/factory/`:
idempotent runs keyed by a run key, run state and audit as S3 objects under the state prefix of each environment
(platform settings), secrets from Vault through the project's token, retries classified by failure class
(exit codes: 0 ok, 65 data, 70 internal, 75 transient, 77 security, 78 configuration).

Queue-triggered pipelines poll their queue on every DAG run; a message is deleted when its run finished, and a run
that keeps failing ends in the queue's dead-letter queue.
"""


DEPLOYMENT = """# Deployment

Deployment goes through the DataOps deployment service (see its Swagger page for the current URLs):

1. **Sync code**: push the branch, then sync it from GitHub to the deployment bucket (always).
2. **Build the Docker image**: only when `docker/`, `pyproject.toml` or `uv.lock` changed (for example after
   upgrading Pipeline Factory adds a dependency).
3. **Deploy the DAG**: register or update `dags/<dag>.json` in the target environment (`dev`, `sit`, `ppt`, `prd`).

Generated pipelines need nothing else: their configuration per environment is `src/<pipeline>/config_<env>.json`,
and their run state lives in the environment's state bucket (platform settings).
"""


def files(ds: DataopsRepo, team: str) -> dict[str, str]:
    return {
        "pyproject.toml": pyproject(ds),
        "docker/Dockerfile": dockerfile(ds),
        "docker/workdir/entrypoint.py": entrypoint(ds),
        ".github/workflows/ci.yaml": CI,
        ".pre-commit-config.yaml": PRE_COMMIT,
        "sonar-project.properties": sonar(ds),
        "README.md": readme(ds, team),
        "AGENTS.md": agents(ds),
        "docs/ARCHITECTURE.md": architecture(ds),
        "docs/DEPLOYMENT.md": DEPLOYMENT,
        "src/__init__.py": "",
        "src/shared/__init__.py": "",
        "tests/__init__.py": "",
        "dags/.gitkeep": "",
    }


def additions(ds: DataopsRepo) -> list[str]:
    """What an existing repository needs added by hand (its own files are never rewritten)."""
    return [
        "pyproject.toml dependencies: " + ", ".join(runtime_dependencies()),
        f'pyproject.toml [tool.ruff] extend-exclude = ["{VENDOR_GLOB}"]',
        f'pyproject.toml [tool.mypy] exclude += ["{VENDOR_GLOB}/"]; [tool.coverage.run] omit += ["{VENDOR_GLOB}/*"]',
        "pyproject.toml dev dependencies: moto[s3,sqs,secretsmanager] (generated smoke tests; skipped without it)",
        f"sonar exclusions: {VENDOR_GLOB}/**",
        "then rebuild the Docker image once (new dependencies)",
    ]


def platform_template(env: str, team: str, region: str = "<region>") -> dict[str, Any]:
    """A platform settings file to fill in (dataops layout): accounts, the state bucket, Vault."""
    return {
        "settings_version": 1,
        "environment": env,
        "region": region,
        "account_model": "account_per_team",
        "accounts": {team: {"account_id": "<12-digit account id>"}},
        "placements": {
            "dataops": {"account": "@team", "role": "orchestration"},
            "team": {"account": "@team", "role": "workload"},
        },
        "teams": {team: {"account": team}},
        "dataops": {
            "state_bucket": "<bucket for run state, staging and audit>",
            "state_prefix": "_factory/",
            "vault": {"address": "https://<dataops vault host>", "service_env": "prd"},
        },
        "allow_local_secrets": env == "dev",
    }
