# Repository and package structure

## Framework repository (this repo)

```
aisdlc-fivetran-factory-gerter/
  src/pf/, src/pf_runtime/          # pipeline-factory 0.1.0: FROZEN; becomes the `fivetran_sdk` engine backend
  plugin/, steps-plugin/            # pf 0.1.0 Claude Code plugin and headless steps plugin (kept)
  examples/pipelines/…              # pf 0.1.0 examples (kept; CI keeps them green)
  dryrun/                           # pf 0.1.0 dry-run harness (kept; reused by testing/)
  packages/                         # NEW: uv workspace, virtual root packages/pyproject.toml (pf is not a member)
    factory_core/                   # stdlib + pydantic only: errors, retry, canonical json/sha, cel, yamlio, jsonlog+redaction
    factory_spec/                   # spec v1 models, per-category and per-op unions, schema export, migrate/
    factory_runtime/                # run.py, lambda_handler.py, context.py, plugin_api.py
      ops/                          #   portal, discover, transfer, package, crypto, checksum, s3, sqs, secret, http, naming
      sources/                      #   sftp, ftps, https, s3 (all with open_stream())
      secrets/                      #   provider, vault, aws_sm, cache, audit
      state/, manifest.py, images/  #   run-state (DynamoDB), manifest, pod/Lambda Dockerfiles
    factory_airflow/                # pod-operator subclass (exit-code map), callbacks, run-pin helpers; MWAA constraints only
    factory/                        # Typer CLI and build-time logic
      validate/, plan/              #   validator; engine, dataflow, iam, network, placement planners
      generate/templates/           #   Jinja templates: dag, lambda, k8s, docs, ci
      generate/tfjson.py            #   per-pipeline Terraform as canonical .tf.json
      change/, cost/, verify.py     #   change engine, cost model (pricing.v1.json), drift check
      agents/, orchestrator/        #   AgentRunner and agents; deterministic gate state machine
  skills/                           # NEW: SKILL.md per agent capability (spec-authoring, rule-review, …)
  starters/                         # NEW: README only; the 4 starters ship as package data in packages/factory/src/factory/starters/
  testing/                          # NEW: mock portal, Vault dev, SFTP/FTPS/HTTPS stand-ins, moto, Airflow DAG harness
  toolchain/                        # NEW: toolchain Dockerfile + toolchain.lock.json (Phase 2+)
  platform/                         # NEW: example of the platform repo content (modules, MWAA, publisher, settings)
  examples/team-pipelines/          # NEW: example of a team pipelines repo (the worked example, Phase 2+)
  docs/architecture/, docs/adr/     # NEW: this architecture
  tests/                            # pf 0.1.0 tests (kept) + new suites per package
```

The `pf`-to-`factory` relationship is described in [ADR-0001](../adr/0001-framework-evolution.md) and the reuse
inventory in [reuse.md](reuse.md).

## Team pipelines repository

```
<team>-pipelines/
  factory.toml                       # framework version, environments [dev,test,ppt,prod], team, platform-settings path
  pipelines/<pipeline_id>/
    spec/pipeline.yaml               # data engineer
    spec/rules.yaml                  # BA / product owner
    spec/integrations.yaml           # data engineer
    spec/security.yaml               # governance / QA
    spec/environments.yaml           # platform
    spec/destinations.yaml           # governance (destination allow-list per environment)
    spec.lock.json                   # canonical merged spec (generated, committed)
    rules/<rule_id>/impl.py          # custom rule code (agent-authored, reviewed, committed)
    rules/<rule_id>/test_impl.py
    rules/<rule_id>/impl.lock.json   # contract hash, review record, acceptance digest
    fixtures/                        # synthetic only
    approvals/                       # acceptance records
    changes/<n>.json                 # change requests, plans and approvers
    audit/agent-decisions.jsonl      # agent decisions
    build/                           # generated: dag/, bundle/, lambda/, k8s/, fivetran/, terraform/<env>/main.tf.json, docs/, BUILD-MANIFEST.json
  .github/workflows/                 # generated per pipeline
  .github/CODEOWNERS                 # managed blocks: one owner per spec file
```

## Platform repository

```
platform/
  modules/                           # semver-tagged Terraform modules (hand-written HCL)
    mwaa-env, eks-cluster, eks-team-namespace (quota, NetworkPolicy, admission), vpc-endpoints, egress-firewall,
    vault-aws-auth, kms-team-key, run-state-table, staging-bucket, bundle-store, audit-store,
    pipeline-stack, workload-role, lambda-fn, sqs-dlq, s3-target, fivetran-connection, alarms
  live/<stage>/                      # network, mwaa, eks, vault-auth, stores
  mwaa/<stage>/                      # requirements.txt (locked against the constraints), plugins/, startup.sh
  dag-publisher/                     # verifies each DAG, then writes dags/<team>/ in the MWAA bucket
  platform-settings/<stage>.json     # account model, placement, shared ARNs: an input to the generators
```

## Why three repositories

- **Team repos:** GitHub read access is the only real read boundary for specs, which hold study ids and server
  paths. The GitHub OIDC `sub` claim maps one-to-one to a team's deploy role.
- **Platform repo:** holds the shared MWAA environment and the modules that every team depends on, which no single
  team should change.
- **Framework repo:** holds the versioned tool that team and platform repos pin in `factory.toml`.
