"""Audit of agent decisions: one JSON line per agent call in pipelines/<id>/audit/agent-decisions.jsonl (committed).

Hashes and verdict summaries only: no prompts, no requirement text, no code."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .runner import Provenance


class AuditTrail:
    def __init__(self, pipeline_dir: Path, clock: Any = time.time) -> None:
        self.path = pipeline_dir / "audit" / "agent-decisions.jsonl"
        self.clock = clock

    def record(self, agent: str, prov: Provenance | None, *, attempt: int, verdict: str, **summary: Any) -> None:
        doc: dict[str, Any] = {"ts": int(self.clock()), "agent": agent, "attempt": attempt, "verdict": verdict}
        if prov is not None:
            keep = {
                "mode",
                "provider",
                "logical_model",
                "provider_model",
                "skill_sha256",
                "prompt_sha256",
                "schema_sha256",
                "output_sha256",
                "cost_usd",
            }
            doc.update(prov.model_dump(include=keep))
        doc.update(summary)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, sort_keys=True) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]
