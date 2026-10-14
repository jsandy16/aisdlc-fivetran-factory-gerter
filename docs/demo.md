# Pipeline Factory demo: spec to delivered files, then two changes (local, about 20 seconds)

```bash
cd packages
uv sync --all-packages
uv run python -m factory_testing.demo --out /tmp/factory-demo     # prints 14 steps; writes DEMO-REPORT.md
```

Everything runs on this machine, in one process. AWS is moto; the portal and Vault are TLS stand-ins; the partner SFTP
server uses the seeded dev host key. Data and secrets are synthetic. The demo drives the real `factory` CLI and the
**generated** artefacts: the dispatcher and the Lambda tasks take their settings from the generated Terraform, the pod
task from the generated DAG, and the task commands come from the generated DAG source, rendered as Airflow would.
The scratch repository stays in `--out/repo` afterwards, so you can open every file the steps mention.

## What to show, in order

| # | Step | Point to make | Where to look |
|---|---|---|---|
| 1–2 | The worked example in a team repo; `validate → test → build → verify` | A BA-readable spec becomes 23 generated files (DAGs, Terraform, docs) with a manifest; `verify` proves the build is the spec | `repo/pipelines/study_pkg_delivery/spec/`, `repo/build/study_pkg_delivery/` |
| 3 | Stand-ins come up | No cloud account and no real data; secrets live only in the stand-in stores | — |
| 4 | Run 1: a message arrives; the dispatcher starts `study_pkg_delivery`; t1 → t2 → t3 run; then the same run again | The portal picks destinations x (S3) and y (SFTP); both get identical files; a rerun does nothing (exit 0, `already_done`) | report step 4 |
| 5 | **Change 1** (non-breaking): "Partners only need SHA-256 checksum files, not MD5." | The change plan names the spec paths, keeps the DAG, lists the task to retest and asks **governance** to approve (security policy changed); `changes/0001.json` records it | `repo/pipelines/study_pkg_delivery/changes/0001.json` |
| 6–8 | Rebuild, verify, commit; Run 2 | Same DAG, new bundle; the `.md5` file is gone at x and y | report step 7 |
| 9 | **Change 2** (breaking) at 0.3.0: "key runs by study too" | Refused: changing the run key changes run identity, so it needs a major version; nothing is recorded | report step 9 |
| 10–11 | The same change at 2.0.0 | New DAG `study_pkg_delivery__v2`; the old one is paused, drained and retired; **platform** approves | `build/.../dags/dev/study_pkg_delivery__v2.py` |
| 12–13 | Run 3 | The dispatcher (configured by the regenerated Terraform) now starts `__v2`; the run id carries the study | report step 12 |
| 14 | Evidence | Audit objects written, every run outcome logged, and no secret value in any log line (canary check) | `build/.../docs/runbook.md`, `lineage.md`, `alerts.md` |

## Questions to expect

- *Is anything hand-wired?* No. The demo reads settings and commands from the generated files only. If generation
  were wrong, the demo would fail (it found one such gap while it was being built: pods had no settings).
- *What about AWS?* The same artefacts are deployed and run on real MWAA, EKS and Lambda in Phase 8.
- *Who approves?* The plan only lists approvers. People approve on the PR, and CI fills `approvers` in the changelog.
