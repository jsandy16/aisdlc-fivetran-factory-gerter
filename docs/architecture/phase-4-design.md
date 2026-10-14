# Phase 4 design: agents and gates (approved plan of record)

Agents draft specs and custom rules; deterministic code and a person decide what is accepted. Agents see specs,
schemas, metadata and synthetic samples only ([ADR-0019](../adr/0019-agents-and-orchestrator.md),
[ADR-0022](../adr/0022-llm-boundary.md), [agents-and-gates.md](agents-and-gates.md)).

| # | Milestone | Proven by |
|---|---|---|
| M0 | Phase 3 leftovers: s3.list lists the single object an S3 event names; short ADR/doc amendments | `s3_to_s3_event` starter end to end |
| M1 | `factory_agents`: `AgentRunner` on the Claude Agent SDK (isolated session, own skill only, typed output, budget, turn limit; Anthropic API or Bedrock); record/replay with tamper detection | recordings only; no live model calls in CI |
| M2 | LLM boundary: requirement text redacted and fenced, synthetic inputs only, prompt-injection corpus (R10) | every attack blocked or escalated |
| M3 | Intake, Rule Implementation, Rule Review, Test agents; ≤ 3 tries, early stop on a repeated error, typed `Escalation`; audit JSONL | synthetic requirement → valid spec |
| M4 | Custom-rule gates: `test_impl.py`, mypy strict, bandit, review verdict, `factory accept` (person digest over contract, plain English, code sha, tests sha) | unreviewed or changed rules cannot be accepted; accepted rules run |
| M5 | Orchestrator from intake to unit tests, slim review package, `factory agent intake / implement / review` | one replayed run, requirement → accepted custom rule |

Defaults agreed: hand-written synthetic recordings (live re-recording once an API key is provided); mutmut opt-in,
not a gate; Anthropic API by default, Bedrock as a setting. Moved to Phase 5: generation, Terraform, integration
tests, cost, the interactive BA plugin, `factory bundle`, `factory test`.

## As built

- **Package** `packages/factory_agents`: `runner` (isolated session, record/replay, tamper check), `boundary`
  (typed prompt parts, fencing, secret redaction, synthetic samples only), `agents` (typed answers), `loop`
  (≤ 3 attempts, early stop, `Escalation`), `audit` (`pipelines/<id>/audit/agent-decisions.jsonl`, hashes only),
  `intake`, `rules` (implement → gates → review), `gates`, `accept`, `orchestrator`; four headless skills in
  `factory_agents/plugin/skills/`.
- **Live calls** are not exercised in CI (no API key here): the SDK is the optional extra `factory-agents[live]`
  (claude-agent-sdk 0.2.165, as pf); Bedrock needs `FACTORY_AGENT_BEDROCK_MODEL` per account (never guessed).
- **Replay semantics:** an identical request replays the identical answer, so an unchanged resubmission gets the
  same review; the implementer must address findings.
- **Intake** can never write a rule's `implementation`, governance, platform or secret paths; custom-rule findings
  are left to the rule flow.
- **Acceptance:** new validator error `custom_rule_unaccepted`; `rules/<id>/impl.lock.json` holds the digest over
  contract, plain English, code and tests, plus the review verdict and the gates that passed; `review.json` and
  `edge_cases.json` sit next to the code. `factory accept` re-runs every gate (mypy strict, bandit, pytest, owner and
  edge cases). It is person-only by convention; the hook that denies it to agent sessions comes with the Phase 5
  interactive plugin. mutmut is not a gate.
- **CLI:** `factory agent intake | implement | run`, `factory accept` (optional extra `factory[agents]`).
- **Open for Phase 5:** live recordings once a key is provided; the interactive plugin and its hooks;
  `factory test` can reuse `factory_runtime.plugins.run_case` for custom rules.
