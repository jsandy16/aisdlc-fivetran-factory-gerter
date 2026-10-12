"""GitHub Actions workflow per pipeline: verify on every pull request; on merge to main, release the versioned
artefact and deploy to each environment in order through GitHub Environments (protect prod with required
reviewers). Deployment follows Fivetran's documented CI/CD pattern: `fivetran deploy ... --yes` with a
configuration assembled from secrets at run time, then the REST API sets the schedule.

Third-party actions are pinned to full commit SHAs (tags are mutable); the version is in the comment. The framework
is installed from `framework_requirement` in pf.toml, so every pipeline repo pins the framework it was built with."""
from __future__ import annotations

import tomllib

from .. import VERSION
from ..bundle import Bundle

CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1"
SETUP_PYTHON = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97  # v7.0.0"
AWS_CREDENTIALS = "aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd  # v6.3.0"


def framework_requirement(root) -> str:
    """pip requirement for the framework, from pf.toml (e.g. a git URL pinned to a tag, or a private index pin)."""
    try:
        cfg = tomllib.loads((root / "pf.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        cfg = {}
    return cfg.get("framework_requirement") or f"pipeline-factory[runtime,fivetran]=={VERSION}"


def _secret_env(keys: list[str]) -> str:
    return "".join(f"          PF_SECRET_{k.upper()}: ${{{{ secrets.PF_{k.upper()} }}}}\n" for k in keys)


def render_workflow(b: Bundle, lock: dict, header: str) -> str:
    assert b.pipeline
    name = b.name
    slug = name.replace("_", "-")
    envs = list(b.pipeline.environments)
    req = framework_requirement(b.path.parent.parent)
    py = b.pipeline.runtime.python_version
    install = f"      - name: Install pipeline-factory\n        run: pip install '{req}'\n"
    setup = (f"      - uses: {CHECKOUT}\n        with:\n          persist-credentials: false\n"
             f"      - uses: {SETUP_PYTHON}\n        with:\n          python-version: '{py}'\n")
    paths = f"      - 'pipelines/{name}/**'\n      - 'pf.toml'\n"
    out = [header, f"name: pf {name}\n\n",
           "on:\n  pull_request:\n    paths:\n", paths,
           "  push:\n    branches: [main]\n    paths:\n", paths,
           "  workflow_dispatch: {}\n\n",
           "permissions:\n  contents: read\n\n",
           f"concurrency:\n  group: pf-{slug}-${{{{ github.ref }}}}\n  cancel-in-progress: false  # never cancel a deploy half-way\n\n",
           "jobs:\n",
           "  verify:\n    runs-on: ubuntu-latest\n    steps:\n", setup, install,
           f"      - name: Validate specs\n        run: pf validate {name}\n"
           f"      - name: Generated files match the specs (no hand edits, nothing stale)\n        run: pf build {name} --check\n"
           f"      - name: Break-and-verify tests on fixtures\n        run: pf test {name}\n"
           f"      - name: Version bump matches the change\n        run: pf release {name} --check\n"]
    prev = "verify"
    for env in envs:
        keys = lock["secrets"].get(env, [])
        out.append(
            f"\n  deploy-{env}:\n"
            f"    needs: {prev}\n"
            "    if: github.event_name == 'push' && github.ref == 'refs/heads/main'\n"
            "    runs-on: ubuntu-latest\n"
            "    permissions:\n      contents: read\n      id-token: write  # OIDC to AWS, deploy jobs only\n"
            f"    environment: {env}  # configure required reviewers for production environments\n"
            "    steps:\n"
            + setup + install +
            f"      - name: AWS credentials (OIDC)\n        uses: {AWS_CREDENTIALS}\n"
            "        with:\n          role-to-assume: ${{ vars.PF_AWS_ROLE_ARN }}\n          aws-region: ${{ vars.PF_AWS_REGION }}\n"
            f"      - name: Store the versioned release artefact (write-once)\n        run: pf release {name} --upload s3://${{{{ vars.PF_ARTEFACT_BUCKET }}}}\n"
            f"      - name: Deploy to Fivetran and apply the schedule\n        run: pf deploy {name} --env {env}\n"
            "        env:\n"
            "          FIVETRAN_API_KEY: ${{ secrets.FIVETRAN_API_KEY }}\n"
            + _secret_env(keys))
        prev = f"deploy-{env}"
    return "".join(out)
