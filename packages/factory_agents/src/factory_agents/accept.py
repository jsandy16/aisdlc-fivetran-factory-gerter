"""A person accepts a custom rule (`factory accept`). Agents never call this; Phase 5's interactive plugin also
denies it to agent sessions with a hook.

Preconditions: every gate passes on the current files, and the latest review approved exactly these files. The
acceptance (impl.lock.json) records the digest over contract, plain English, code and tests, the review verdict,
the gates that passed, who accepted and when.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from factory_spec.acceptance import LOCK, acceptance_digest
from factory_spec.load import load_spec

from .errors import AgentError
from .gates import REVIEW, run_gates


def accept_rule(
    pipeline_dir: Path, rule_id: str, by: str, *, tools: bool = True, clock: Any = time.time
) -> dict[str, Any]:
    if not by.strip():
        raise AgentError("acceptor_missing", "say who accepts the rule (--by)")
    spec = load_spec(pipeline_dir).spec
    rule = spec.rule(rule_id) if spec else None
    if rule is None or rule.implementation.kind != "custom":
        raise AgentError("rule_not_custom", f"{rule_id} is not a custom rule of this pipeline")
    d = pipeline_dir / "rules" / rule_id
    gates = run_gates(pipeline_dir, rule, tools=tools)
    if not gates.ok:
        raise AgentError("gates_failed", "; ".join(gates.issues()))
    review_file = d / REVIEW
    review = json.loads(review_file.read_text(encoding="utf-8")) if review_file.is_file() else {}
    if review.get("verdict") != "approve":
        raise AgentError("review_missing", "the rule has no approving review")
    if (review.get("code_sha256"), review.get("tests_sha256")) != (gates.code_sha256, gates.tests_sha256):
        raise AgentError("review_stale", "the code or tests changed after the review")
    lock = {
        **acceptance_digest(rule, d),
        "review": {"verdict": "approve", "output_sha256": review.get("output_sha256", "")},
        "gates": [c.name for c in gates.checks],
        "accepted_by": by.strip(),
        "accepted_at": int(clock()),
    }
    (d / LOCK).write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return lock
