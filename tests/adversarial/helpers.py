"""Shared helpers for the adversarial suites: minimal locks, a local-folder file source, a scripted RDS session."""
from __future__ import annotations

import copy
import os
import time
from pathlib import Path
from typing import Any

from pf.testing import LocalDirSession, NullLog, RecorderOp
from pf_runtime import engine

FUTURE = time.time() + 10 * 86400


def attr(name: str, type_: str = "STRING", **kw) -> dict:
    a = {"name": name, "source": kw.pop("source", None), "type": type_, "nullable": kw.pop("nullable", True),
         "transforms": kw.pop("transforms", []), "checks": kw.pop("checks", []), "on_type_error": kw.pop("on_type_error", "quarantine_row"),
         "input_format": kw.pop("input_format", None), "precision": kw.pop("precision", None), "scale": kw.pop("scale", None)}
    a.update(kw)
    return a


def chk(id_: str, kind: str, **kw) -> dict:
    c = {"id": id_, "kind": kind, "mode": kw.pop("mode", "enforce"), "on_fail": kw.pop("on_fail", "quarantine_row"), "max_failed_pct": kw.pop("max_failed_pct", None)}
    c.update(kw)
    return c


def file_entity(name: str = "t", attrs: list[dict] | None = None, member: str = r".*\.csv", load: str = "append", pk=None,
                fmt: dict | None = None, checks=None, row_checks=None, extra_columns: str = "reject") -> dict:
    return {"name": name, "file": {"member": member, "format": {"type": "csv", "delimiter": ",", "quotechar": '"', "header": True,
                                                                  "encoding": "utf-8", "null_tokens": [""], **(fmt or {})},
                                   "extra_columns": extra_columns},
            "load": load, "primary_key": pk or [], "attributes": attrs or [attr("id"), attr("v")],
            "checks": checks or [], "row_checks": row_checks or []}


def file_lock(entities: list[dict] | None = None, package: dict | None = None, secrets: list[str] | None = None) -> dict:
    pkg = {"match": r"(?P<name>[^/]+\.csv)", "order_by": [], "readiness": {"min_age_seconds": 0, "marker": None},
           "on_unmatched": "warn", "on_redelivery": "reject", "max_package_bytes": 10**9, "transforms": [], "checks": []}
    pkg.update(package or {})
    return {"pipeline": "adv", "version": "1.0.0", "spec_sha256": "x", "environments": ["dev"],
            "secrets": {"dev": secrets or []}, "archive_originals": False, "originals": {},
            "runtime": {"parallelism": 2, "retry": {"attempts": 2, "base_seconds": 0.0, "max_seconds": 0.0}, "checkpoint_every_rows": 1000},
            "source": {"type": "sftp", "package": pkg}, "entities": entities or [file_entity()]}


def rds_lock(entities: list[dict], batch_size: int = 2) -> dict:
    lk = file_lock()
    lk["source"] = {"type": "rds", "engine": "postgres", "batch_size": batch_size}
    lk["entities"] = entities
    return lk


def run(lock: dict, root: Path | None = None, state: dict | None = None, cfg: dict | None = None, factory=None, now: float = FUTURE):
    op = RecorderOp(engine.schema(lock))
    config = {"pf_environment": "dev", **(cfg or {})}
    for k in lock["secrets"].get("dev", []):
        config.setdefault(k, "secret-value")
    if factory is None:
        factory = lambda src, c: LocalDirSession(root)  # noqa: E731
    engine.update(copy.deepcopy(lock), config, copy.deepcopy(state or {}), op, NullLog(), run_id="r1", now=lambda: now,
                  sleep=lambda s: None, session_factory=factory)
    return op


def last_state(op: RecorderOp) -> dict:
    return op.checkpoints[-1] if op.checkpoints else {}


def audits(op: RecorderOp) -> list[tuple[str, str, str]]:
    return [(a["package"], a["status"], a["rule_id"]) for a in op.rows.get("pf_package_audit", [])]


def rows(op: RecorderOp, table: str = "t") -> list[dict]:
    return op.rows.get(table, [])


def write(root: Path, rel: str, data: bytes | str, mtime: float | None = None) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode() if isinstance(data, str) else data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p
