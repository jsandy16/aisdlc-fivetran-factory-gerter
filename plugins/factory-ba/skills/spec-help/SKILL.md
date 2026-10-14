---
name: spec-help
description: Help a business analyst read, change and check a Pipeline Factory spec through the `factory --json` CLI, one topic at a time, in plain English.
---
# Working on a spec

- Find the pipeline: `factory --json show <pipeline> --view ba` gives the plain-English view. Explain it in plain words.
- Change the spec only with `factory --json set|add|remove <pipeline> <file> <path> [value]`: an edit that adds
  validation errors is rolled back and the errors come back in the envelope. Never edit spec files by other means.
- After changes: `factory --json validate <pipeline>`, then `factory --json lock <pipeline>`.
- Ask one question at a time; never fill governance, security, destinations, environments or secrets yourself:
  those belong to governance and the platform team.
- Useful checks: `factory --json test`, `plan`, `cost`, `build` (the output under `build/` is generated: never edit it).
- Before a change ships: `factory --json change plan <pipeline> --base origin/main` explains it in full (what
  changed, breaking or not, files, infrastructure, rules to re-review, cost delta, approvals); `factory --json verify`
  proves the build matches the spec.
- Local runs: `factory --json run <pipeline> --message msg.json` (dev only). The developer fills secrets in
  their own file (`factory --json secrets template <pipeline>`); never read, print or ask for that file.
- Extra AWS services (topics, alarms, dashboards, rules, buckets, queues, tables) are `environments.yaml → resources`
  entries from the resource catalog. Who creates them is `infrastructure`, which is platform-owned, so ask; don't set it.
- `factory accept` is for a person to run after reading the review package. Never run it; the hooks refuse it.
- Never print, read or ask for secret values. Specs hold references only.
