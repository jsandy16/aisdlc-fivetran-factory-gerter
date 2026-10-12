"""`pf test` — break-and-verify on the pipeline's own synthetic fixtures, with the real runtime.

fixtures/good/...            deliveries (file sources) or <entity>.csv rows (rds) that must load cleanly
fixtures/bad/<case>/...      each case breaks something; fixtures/bad/<case>/expect.yaml says what must happen:
    status: rejected | failed | loaded      # outcome of the delivery (rds: of the sync)
    rule_id: <id>                           # the rule that must fire (rejected/failed)
    quarantined: {<rule_id>: <rows>}        # rows that must be quarantined by each rule
Coverage: every enforced check should fire in at least one bad case ("a check you have never seen fail is a
check you cannot trust"). Uncovered checks are errors for GxP tiers and warnings otherwise.
"""
from __future__ import annotations

import copy
import csv
import json
import shutil
import time
from pathlib import Path
from typing import Any

from . import yamlio
from .bundle import Bundle
from .envelope import Issue, PfError


class RecorderOp:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {}
        self.checkpoints: list[dict] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.truncated: list[str] = []

    def upsert(self, table: str, data: dict) -> None:
        json.dumps(data)  # emitted values must be JSON-serialisable, like the real SDK
        self.rows.setdefault(table, []).append(data)

    def update(self, table: str, modified: dict) -> None:
        self.upsert(table, modified)

    def delete(self, table: str, keys: dict) -> None:
        self.rows.setdefault(f"{table}__deleted", []).append(keys)

    def truncate(self, table: str) -> None:
        self.truncated.append(table)

    def checkpoint(self, state: dict) -> None:
        self.checkpoints.append(json.loads(json.dumps(state)))

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str, trace: str | None = None) -> None:
        self.errors.append(message)


class NullLog:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, m: str) -> None:
        self.lines.append(m)

    warning = error = severe = fine = debug = info


class LocalDirSession:
    """A file source backed by a local folder (fixtures). Same interface as SftpSession/FtpSession."""

    def __init__(self, root: Path):
        self.root = root

    def list_files(self):
        from pf_runtime.sources import RemoteFile

        out = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and p.name != "expect.yaml":
                st = p.stat()
                out.append(RemoteFile(p.relative_to(self.root).as_posix(), st.st_size, st.st_mtime))
        return out

    def download(self, rel: str, dest: str) -> None:
        shutil.copyfile(self.root / rel, dest)

    def read_text(self, rel: str, limit: int = 1 << 20):
        p = self.root / rel
        return p.read_bytes()[:limit].decode("utf-8", errors="replace") if p.exists() else None

    def close(self) -> None:
        pass


class CsvRdsSession:
    """A database source backed by <entity>.csv fixtures (header row = source column names)."""

    def __init__(self, root: Path, lock: dict):
        self.root = root
        self.by_table = {e["table"]["table"]: e["name"] for e in lock["entities"]}

    def _file(self, table: str) -> Path:
        return self.root / f"{self.by_table[table]}.csv"

    def columns(self, table: str) -> set[str]:
        f = self._file(table)
        if not f.exists():
            return set()
        with open(f, newline="", encoding="utf-8") as fh:
            return set(next(csv.reader(fh), []))

    def read(self, table, columns, cursor_column, since, order_by, batch_size):
        f = self._file(table)
        with open(f, newline="", encoding="utf-8") as fh:
            rows = [{c: (r.get(c) if r.get(c) != "" else None) for c in columns} for r in csv.DictReader(fh)]
        if cursor_column:
            rows = [r for r in rows if since is None or (r[cursor_column] is not None and str(r[cursor_column]) >= str(since))]
            rows.sort(key=lambda r: (str(r[cursor_column]), *[str(r[c]) for c in order_by]))
        for i in range(0, len(rows), batch_size):
            yield rows[i:i + batch_size]

    def close(self) -> None:
        pass


def _config(lock: dict) -> dict:
    env = lock["environments"][0]
    cfg = {"pf_environment": env}
    for k in lock["secrets"].get(env, []):
        cfg[k] = "pf-test-secret-value"
    return cfg


def run_case(lock: dict, case_dir: Path, state: dict | None = None) -> tuple[RecorderOp, dict]:
    from pf_runtime import engine

    lk = copy.deepcopy(lock)
    lk["archive_originals"] = False  # fixtures never leave the machine
    op, log = RecorderOp(), NullLog()
    if lk["source"]["type"] in ("sftp", "ftp"):
        factory = lambda src, cfg: LocalDirSession(case_dir)  # noqa: E731
    else:
        factory = lambda src, cfg: CsvRdsSession(case_dir, lk)  # noqa: E731
    future = time.time() + 10 * 86400  # fixtures are always "old enough" for readiness checks
    engine.update(lk, _config(lk), state or {}, op, log, run_id="test", now=lambda: future, sleep=lambda s: None, session_factory=factory)
    return op, {"log": log.lines}


def _outcome(op: RecorderOp) -> dict:
    audits = op.rows.get("pf_package_audit", [])
    q: dict[str, int] = {}
    for r in op.rows.get("pf_quarantine", []):
        q[r["rule_id"]] = q.get(r["rule_id"], 0) + 1
    fired = {r["rule_id"] for r in op.rows.get("pf_rule_results", []) if r["failed"] > 0 and r["mode"] == "enforce"}
    return {"statuses": [(a["package"], a["status"], a["rule_id"]) for a in audits], "quarantined": q, "fired": fired, "errors": op.errors}


def enforced_checks(lock: dict) -> set[str]:
    ids = set()
    if lock["source"]["type"] in ("sftp", "ftp"):
        ids |= {c["id"] for c in lock["source"]["package"]["checks"] if c.get("mode") == "enforce" and c.get("on_fail") != "warn"}
    for e in lock["entities"]:
        ids |= {c["id"] for c in e.get("checks", []) if c.get("mode") == "enforce" and c.get("on_fail") != "warn"}
        ids |= {c["id"] for c in e.get("row_checks", []) if c.get("mode") == "enforce" and c.get("on_fail") != "warn"}
        for a in e["attributes"]:
            ids |= {c["id"] for c in a.get("checks", []) if c.get("mode") == "enforce" and c.get("on_fail") != "warn"}
    return ids


def run_tests(root: Path, b: Bundle) -> dict:
    build = root / "pipelines" / b.name / "build"
    if not (build / "spec.lock.json").exists():
        raise PfError("no build found; run `pf build` first", "not_built")
    lock = json.loads((build / "spec.lock.json").read_text(encoding="utf-8"))
    fx = b.path / "fixtures"
    good = fx / "good"
    issues: list[Issue] = []
    cases: list[dict[str, Any]] = []
    if not good.is_dir() or not any(p.is_file() for p in good.rglob("*")):
        issues.append(Issue(file="fixtures/good", msg="no good fixtures: add synthetic deliveries that must load", code="no_fixtures"))
    else:
        op, _ = run_case(lock, good)
        oc = _outcome(op)
        bad = [s for s in oc["statuses"] if s[1] != "loaded"]
        if bad or oc["quarantined"] or oc["errors"]:
            issues.append(Issue(file="fixtures/good", code="good_failed",
                                msg=f"good fixtures did not load cleanly: statuses={bad} quarantined={oc['quarantined']} errors={oc['errors']}"))
        # idempotency: a second sync with the final state must emit nothing new
        if op.checkpoints:
            op2, _ = run_case(lock, good, state=op.checkpoints[-1])
            data_rows = sum(len(v) for t, v in op2.rows.items() if not t.startswith("pf_"))
            if data_rows and lock["source"]["type"] in ("sftp", "ftp"):
                issues.append(Issue(file="fixtures/good", code="not_idempotent", msg=f"re-running with the saved state emitted {data_rows} rows again"))
        cases.append({"case": "good", **{k: (sorted(v) if isinstance(v, set) else v) for k, v in oc.items()}})
    fired_all: set[str] = set()
    bad_root = fx / "bad"
    for case in sorted(p for p in bad_root.iterdir() if p.is_dir()) if bad_root.is_dir() else []:
        exp_f = case / "expect.yaml"
        if not exp_f.exists():
            issues.append(Issue(file=f"fixtures/bad/{case.name}", msg="expect.yaml missing", code="no_expectation"))
            continue
        exp = yamlio.to_plain(yamlio.load(exp_f)) or {}
        op, _ = run_case(lock, case)
        oc = _outcome(op)
        fired_all |= oc["fired"]
        ok = True
        want_status = exp.get("status")
        if want_status:
            got = {s[1] for s in oc["statuses"]} | ({"failed"} if oc["errors"] else set())
            if want_status not in got:
                ok = False
        if exp.get("rule_id") and exp["rule_id"] not in oc["fired"] and exp["rule_id"] not in {s[2] for s in oc["statuses"]}:
            ok = False
        for rid, n in (exp.get("quarantined") or {}).items():
            if oc["quarantined"].get(rid, 0) != n:
                ok = False
        if not ok:
            issues.append(Issue(file=f"fixtures/bad/{case.name}", code="expectation_failed",
                                msg=f"expected {exp}, got statuses={oc['statuses']} quarantined={oc['quarantined']} errors={oc['errors']}"))
        cases.append({"case": case.name, "expected": exp, **{k: (sorted(v) if isinstance(v, set) else v) for k, v in oc.items()}})
    uncovered = sorted(enforced_checks(lock) - fired_all)
    if uncovered:
        sev = "error" if lock["gxp_tier"] != "non_gxp" else "warning"
        issues.append(Issue(severity=sev, file="fixtures/bad", code="uncovered",
                            msg=f"these checks never fired in a bad fixture (break-and-verify): {uncovered}"))
    ok = not any(i.severity == "error" for i in issues)
    report = {"pipeline": b.name, "spec_sha256": lock["spec_sha256"], "ok": ok, "cases": cases, "uncovered": uncovered}
    (build / "TEST-REPORT.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return {"report": report, "issues": issues}

