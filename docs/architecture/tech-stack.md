# Tech stack and version pins

The brief's stack is confirmed with the changes listed below. **Pins are proposals.** They are checked again and
frozen in `toolchain/toolchain.lock.json` (build-time) and the per-target dependency locks (`factory deps lock`,
[ADR-0017](../adr/0017-determinism.md)) when the matching phase starts.

Versions were checked on 2026-10-13: Python packages against PyPI, Airflow providers against the official
`constraints-3.3.1` files. Terraform and provider versions could not be reached from the build environment and are
marked *verify*.

## Build-time (framework, toolchain image)

| Component | Proposed pin | Notes |
|---|---|---|
| Python | **3.12** | One minor version everywhere: toolchain, pod image, Lambda image, MWAA. MWAA's Python version for Airflow 3.3.1 is confirmed in spike S1. pf 0.1.0 declares `>=3.10` but needs 3.11 (`tomllib`); fixed in Phase 2. |
| Packaging / workspace | uv 0.13.x | uv workspace for `packages/*`; `pf` stays a setuptools project in `src/`. |
| Pydantic | 2.13.x (2.14.1 latest) | Pinned to the version in the MWAA constraints for code shared with Airflow. JSON Schema output changes between minors, so the pin is part of the build hash. |
| Claude Agent SDK (Python) | 0.2.165 | Same as pf 0.1.0; upgraded deliberately with re-recorded fixtures. |
| Jinja2 | 3.1.6 | `StrictUndefined`, no autoescape, `keep_trailing_newline`. |
| ruamel.yaml | 0.18.x / 0.19.1 | Round-trip with comments, as in pf. Aliases are rejected. |
| Typer | 0.27.x | `factory` CLI. pf keeps argparse. |
| httpx | 0.28.1 | Portal and REST client (also in the MWAA constraints). |
| hvac | 2.4.0 | Vault client (also in the MWAA constraints). |
| boto3 / botocore | per-target lock | MWAA constraints pin 1.43.56; pf connectors pin 1.43.111. Each target gets its own lock. |
| aws-encryption-sdk | 4.0.x | Package encryption ([ADR-0012](../adr/0012-encryption.md)). |
| cel-python | 0.5.0 | Same engine as pf (null-aware wrappers in `pf_runtime/cel.py`). |
| jmespath | 1.1.0 | The only response-mapping dialect ([ADR-0009](../adr/0009-portal-integration.md)). |
| pytest / moto | 9.1.x / 5.2.x | moto as both an in-process mock and a server. |
| ruff / mypy | 0.17.x / 2.4.x | `mypy --strict` for new packages. |
| bandit / mutmut | 1.9.x / 3.8.x | Custom-rule review gates ([ADR-0020](../adr/0020-custom-rule-plugins.md)). |
| python-hcl2 | 8.1.x | Parses the hand-written module HCL in tests. |

## Orchestration (MWAA)

| Component | Proposed pin | Notes |
|---|---|---|
| Apache Airflow | **3.3.1** (fallback 3.2) | [ADR-0007](../adr/0007-airflow-version.md); spike S1. |
| Constraints | `constraints-3.3.1/constraints-3.12.txt` | `requirements.txt` is validated against it in CI. |
| apache-airflow-providers-amazon | 9.34.0 (from constraints) | `EksPodOperator`, `LambdaInvokeFunctionOperator`, deferrable triggers. |
| apache-airflow-providers-cncf-kubernetes | 10.21.0 (from constraints) | Pod operator base. |
| apache-airflow-providers-hashicorp | 4.8.0 (from constraints) | Not used for pipeline secrets ([ADR-0008](../adr/0008-secret-provider.md)); listed only because the brief names it. |
| apache-airflow-providers-common-messaging | 2.0.4 (from constraints) | Evaluated; not used for GxP event custody ([ADR-0010](../adr/0010-event-triggers-and-sqs-custody.md)). |
| factory_airflow | framework release | Its only dependencies are packages already in the constraints file. |

## Run-time images

| Image | Base | Contents |
|---|---|---|
| `factory-runtime` (EKS) | distroless/slim Python 3.12, by digest | `factory_runtime`, `factory_core`, the allow-listed libraries for custom rules; read-only root, non-root user |
| `factory-runtime-lambda` | AWS Lambda Python 3.12 base, by digest | same packages; handler `factory_runtime.lambda_handler.handle` |

Both images are built reproducibly where possible, signed (AWS Signer or cosign), and referenced **only by digest**.

## Infrastructure

| Component | Proposed pin | Notes |
|---|---|---|
| Terraform | **≥ 1.10**, exact patch in `toolchain.lock` (*verify*) | 1.10+ supports S3-native state locking (`use_lockfile`), so no DynamoDB lock table is needed. |
| hashicorp/aws provider | 6.x (pf 0.1.0 uses `~> 6.68`) (*verify*) | Multi-platform lock hashes are committed. |
| hashicorp/vault provider | 5.x (*verify*) | Vault auth roles and policies (platform repo). |
| fivetran/fivetran provider | 1.x (*verify*) | `fivetran_native` engine. Connector credentials are set through the REST API, not Terraform state. |
| tflint / checkov | latest at Phase 5 (checkov 3.3.28 today) | CI gates. |
| Kubernetes policy | ValidatingAdmissionPolicy (built in) or Kyverno | [ADR-0006](../adr/0006-mwaa-control-plane.md). |
| Karpenter | platform-owned | on-demand and spot node pools; scales to zero. |

## Changes to the brief's stack

| Brief | Proposed | Why |
|---|---|---|
| "Airflow providers … hashicorp" | Not used for pipeline secrets | Keeps Vault out of Airflow's availability path ([ADR-0008](../adr/0008-secret-provider.md)). |
| PyYAML/ruamel.yaml | ruamel.yaml only | Round-trip with comments is needed for agent spec patches; one parser. |
| JSONPath or JMESPath | JMESPath only | One dialect, already pinned, deterministic. |
| IRSA | Pod Identity recommended, IRSA supported | [ADR-0011](../adr/0011-workload-identity.md). |
| — | uv workspace, toolchain image | Byte-identical generation on any machine ([ADR-0017](../adr/0017-determinism.md)). |
