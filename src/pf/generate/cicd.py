"""GitHub Actions workflow per pipeline: verify on every pull request; on merge to main, release the versioned
artefact and deploy to each environment in order through GitHub Environments (protect prod with required
reviewers). Deployment follows Fivetran's documented CI/CD pattern: `fivetran deploy ... --yes` with a
configuration assembled from secrets at run time, then the REST API sets the schedule and unpauses."""
from __future__ import annotations

from ..bundle import Bundle


def _secret_env(keys: list[str]) -> str:
    return "".join(f"          PF_SECRET_{k.upper()}: ${{{{ secrets.PF_{k.upper()} }}}}\n" for k in keys)


def render_workflow(b: Bundle, lock: dict, header: str) -> str:
    assert b.pipeline
    name = b.name
    slug = name.replace("_", "-")
    envs = list(b.pipeline.environments)
    paths = f"      - 'pipelines/{name}/**'\n      - 'src/**'\n      - 'pyproject.toml'\n"
    out = [header, f"name: pf {name}\n\n",
           "on:\n  pull_request:\n    paths:\n", paths,
           "  push:\n    branches: [main]\n    paths:\n", paths,
           "  workflow_dispatch: {}\n\n",
           "permissions:\n  contents: read\n  id-token: write  # OIDC to AWS for artefact upload\n\n",
           f"concurrency:\n  group: pf-{slug}-${{{{ github.ref }}}}\n  cancel-in-progress: false  # never cancel a deploy half-way\n\n",
           "jobs:\n",
           "  verify:\n    runs-on: ubuntu-latest\n    steps:\n"
           "      - uses: actions/checkout@v4\n"
           "      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.12'\n"
           "      - name: Install pipeline-factory\n        run: pip install -e '.[runtime,fivetran,dev]'\n"
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
            f"    environment: {env}  # configure required reviewers for production environments\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.12'\n"
            "      - name: Install pipeline-factory\n        run: pip install -e '.[runtime,fivetran]'\n"
            "      - name: AWS credentials (OIDC)\n        uses: aws-actions/configure-aws-credentials@v4\n"
            "        with:\n          role-to-assume: ${{ vars.PF_AWS_ROLE_ARN }}\n          aws-region: ${{ vars.PF_AWS_REGION }}\n"
            f"      - name: Store the versioned release artefact (write-once)\n        run: pf release {name} --upload s3://${{{{ vars.PF_ARTEFACT_BUCKET }}}}\n"
            f"      - name: Deploy to Fivetran and apply the schedule\n        run: pf deploy {name} --env {env}\n"
            "        env:\n"
            "          FIVETRAN_API_KEY: ${{ secrets.FIVETRAN_API_KEY }}\n"
            + _secret_env(keys))
        prev = f"deploy-{env}"
    return "".join(out)
