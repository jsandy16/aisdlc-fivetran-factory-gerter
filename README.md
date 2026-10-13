# pipeline-factory

> **Repository status (Pipeline Factory v2, in progress).** This repository holds the pipeline-factory framework.
> Version 0.1.0 (the Fivetran Connector SDK factory documented below) is complete and unchanged. It is being
> extended into a multi-engine Pipeline Factory (MWAA, EKS, Lambda, Fivetran) in 7 reviewed phases. Start with
> [docs/architecture/00-overview.md](docs/architecture/00-overview.md) and the ADRs in [docs/adr/](docs/adr/).
> The 0.1.0 walkthrough and dry-run evidence are in [docs/pipeline-factory-end-to-end.md](docs/pipeline-factory-end-to-end.md).

Spec-driven factory for Fivetran ingestion pipelines into an S3 data lake, run as an AI-SDLC: people own six YAML
specs and every decision; the framework generates, tests, versions and deploys everything else, deterministically.

```
requirement doc (optional) ─┐
source type + destination ──┴─► pf new ─► 6 spec files ─► pf validate ─► pf rules compile/accept ─► pf build
                                                                                                     │
          Fivetran Connector SDK connector · post-load checks · Lake Formation/audit Terraform · ODCS contract · CI
                                                                                                     │
                                         pf test (break-and-verify) ─► pf release ─► PR review ─► CI: pf deploy
```

Supported now: sources `sftp`, `ftp` (FTPS), `rds` (PostgreSQL, MySQL); destination `s3` (Fivetran Managed Data Lake).

## Where the LLM is used — and where it is not

| Step | How | Output contract |
|---|---|---|
| Read a requirement document | Claude Agent SDK + skill `extracting-pipeline-requirements` | `RequirementProposal` (strict JSON Schema); merged deterministically into placeholders only, with evidence |
| Plain-English rule → logic | skill `compiling-english-rules` | `CompiledRule` (CEL + boundary cases), verified by evaluating every case |
| Independent back-translation | skill `explaining-compiled-rules` | `Explanation`; shown to the owner, who accepts |
| Everything else (validation, generation, runtime, tests, release, deploy) | plain code | deterministic; `pf build --check` proves it |

English is compiled **at build time** and frozen in `rules.lock.yaml`; nothing is interpreted at runtime. LLM calls
are recorded (`PF_LLM_PROVIDER=record`) and replayed offline in CI (`replay`).

## Install

```bash
pip install -e '.[runtime,fivetran,agent,docs]' \
  -c examples/pipelines/oncology_labs/build/connector/requirements.txt   # pins match the connector, so pf deploy's preflight passes
pf init-repo ../rd-pipelines                        # a pipelines repository (pf.toml, .gitignore, .claude/settings.json)
claude --plugin-dir <framework>/plugin              # run in the pipelines repo: skills, reviewer agent, guard-rail hooks
```

## Quick start

```bash
cd ../rd-pipelines
pf new site_monitoring --source ftp --destination s3 --requirements requirement.md   # drafts values with evidence
pf validate site_monitoring --allow-unfilled      # what owners still have to fill, by file
pf set site_monitoring pipeline.yaml gxp_tier non_gxp                                  # owners fill / change any value
pf add-check site_monitoring --entity site_monitoring --attribute site_no '{id: site_no_format, kind: regex, pattern: "^\d{4}$", description: "4 digits"}'
pf rules compile site_monitoring                  # English → CEL (LLM), with cases and back-translation
pf rules accept site_monitoring report_within_10_days --by @owner                      # a person only
pf build site_monitoring && pf test site_monitoring && pf release site_monitoring
git add -A && git commit && git push              # PR: verify job; merge: deploy through GitHub Environments
```

`pf --json <command>` returns one envelope (`{api_version, command, ok, issues[], data}`) — the contract agents use.
`pf catalog` lists every check and transform; `pf schema` prints the JSON Schema of every spec file.

## Repository layout

| Path | What |
|---|---|
| `src/pf/` | CLI, spec models (pydantic, `extra=forbid`), validation, intake, rule compiler, generators, release, deploy |
| `src/pf_runtime/` | runtime vendored into every connector (sources, package/unzip safety, CEL rules, engine) |
| `plugin/` | interactive Claude Code plugin: 4 skills, the read-only `pipeline-spec-reviewer` agent, guard-rail hooks |
| `steps-plugin/` | headless plugin loaded only by `pf` through the Agent SDK: the 3 LLM-step skills |
| `examples/` | a pipelines repository with three complete pipelines (SFTP zip packages, FTPS CSV, RDS PostgreSQL) |
| `tests/` | pytest suite; `tests/llm-fixtures` recorded LLM answers; `tests/fixture_builders` synthetic data |
| `dryrun/` | end-to-end rehearsal against local SFTP/FTPS/Postgres/S3 stand-ins and the official Fivetran tester |
| `docs/` | [architecture](docs/architecture.md), [operations](docs/operations.md) |

## Dry run (end to end, locally)

```bash
dryrun/up.sh /tmp/pf-dry                          # SFTP (pinned host key), FTPS (TLS), PostgreSQL 16, S3 (moto)
python dryrun/run.py /tmp/pf-dry --llm replay     # or --llm record to call the model and refresh fixtures
```

Runs every example through `fivetran debug` (the official local tester), asserts the warehouse contents, deploys
through the real `fivetran deploy` CLI against a mock Fivetran API, and runs failure drills. Report:
`/tmp/pf-dry/DRY-RUN-REPORT.json`.
