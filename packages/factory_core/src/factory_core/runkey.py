"""Run keys made safe for paths, Airflow run ids and logs.

`run_dir(run_key)` is used by the dispatcher (Airflow `dag_run_id`), the runtime (staging folder, run-state key) and the
Airflow operators, so one function defines it: readable prefix + sha256 suffix, characters [A-Za-z0-9_.-] only (a subset
of Airflow's allowed run-id pattern), at most 213 characters. `run_key_hash` is what logs show instead of the run key.
"""

from __future__ import annotations

import hashlib
import re

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_PREFIX = 200


def run_key_hash(run_key: str) -> str:
    return hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:12]


def run_dir(run_key: str) -> str:
    """'REQ-123/abc' -> 'REQ-123_abc-<sha12>'. Distinct run keys never collide (the hash covers the original)."""
    if not isinstance(run_key, str) or not run_key:
        raise ValueError("a run key must be non-empty text")
    prefix = _UNSAFE.sub("_", run_key)[:_MAX_PREFIX].strip("._-") or "run"
    return f"{prefix}-{run_key_hash(run_key)}"
