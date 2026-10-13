# factory

The `factory` command line. Phase 2 commands work on specs; generation, tests, planning, deployment checks, change
control, docs and cost arrive in Phases 3–6 (see the [overview](../../docs/architecture/00-overview.md)).

| Command | What |
|---|---|
| `factory init [path] --team T --platform-settings P` | create a team pipelines repository (`factory.toml`, `pipelines/`) |
| `factory new <id> --starter S` | copy a starter (package data in `starters/`) and report how many values are left to fill |
| `factory validate <id> [--allow-unfilled]` | load and validate the six spec files; prints `logic_sha` when valid |
| `factory lock <id> [--check]` | write `spec.lock.json`, or fail in CI if it is missing or stale |
| `factory show <id> --view ba\|lock` | plain-English view for the BA, or the canonical lock |
| `factory set\|add\|remove <id> <file> <path> [value]` | structured edits that keep comments; an edit that adds errors is rolled back |
| `factory migrate <id> [--to V] [--dry-run]` | move a spec to a newer schema version |
| `factory schema [--write DIR \| --check]` | export the JSON Schemas, or check the committed ones |

`factory --json <command>` prints exactly one envelope `{api_version, command, ok, factory_version, issues[], data}`,
also on usage errors. Exit codes: 0 ok, 1 the command ran and found problems, 2 usage or environment error.
