# ADR-0020: Custom rules: typed contract, enforced boundaries, review and person acceptance

- **Status:** Accepted
- **Date:** 2026-10-13

## Context

Custom code is the escape hatch for logic the op library cannot express. LLM-written code must be safe and correct.

## Decision

- **Contract:** a plugin implements `CustomRule[Params, Inputs, Outputs]` with declared `side_effects` and `aws_actions`.
- **What `RuleContext` provides:** workdir, typed artefact streams, handles only for declared secrets, log, run key and a pinned `now()`.
- **Boundaries:**
  - an AST import allow-list (no boto3, hvac, socket, subprocess, os.environ, requests);
  - file I/O only in the workdir;
  - egress blocked unless declared;
  - in tests, `ctx` calls are recorded and must be a subset of the declared side effects.
- **Acceptance requires:**
  - `test_impl.py` (BA cases + agent edge cases) passing;
  - mutmut above a threshold;
  - `mypy --strict` and bandit clean;
  - an `approve` from the review agent;
  - a person's acceptance digest over contract + plain English + code sha + tests sha.

## Consequences

- Custom code is reviewable, bounded and traceable.
- In-process Python is not a sandbox; the real boundaries are IAM, network and review.

## Alternatives considered

- *Running plugins in separate sandboxes (gVisor/Firecracker):* deferred, and can be revisited for gxp_direct.
