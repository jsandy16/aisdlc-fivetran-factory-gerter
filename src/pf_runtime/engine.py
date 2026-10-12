"""The sync loop: prepare deliveries in parallel, emit them strictly in order.

Why ordered emission: a Fivetran checkpoint commits every row emitted so far by any thread, so parallel
emission could half-write a delivery that is later rejected, or let an older delivery overwrite a newer row.
Workers therefore only download, check and spool; the main thread alone calls op.*.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import posixpath
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from . import VERSION
from . import package as P
from . import rules as R
from .errors import ConfigError, EntityRejected, PackageRejected, SyncFailed, TransientError
from .logx import Log
from .parse import Reader
from .retry import retry
from .sources import open_file_source
from .values import to_emit

LINEAGE = {
    "pf_package": "STRING", "pf_package_hash": "STRING", "pf_member": "STRING", "pf_row": "LONG",
    "pf_run_id": "STRING", "pf_pipeline_version": "STRING",
}
EVIDENCE_TABLES = [
    {"table": "pf_package_audit", "primary_key": ["pf_run_id", "package_hash", "package"],
     "columns": {"pf_run_id": "STRING", "package_hash": "STRING", "package": "STRING", "pipeline": "STRING",
                 "pipeline_version": "STRING", "runtime_version": "STRING", "spec_hash": "STRING", "environment": "STRING",
                 "package_size": "LONG", "status": "STRING", "rule_id": "STRING", "reason": "STRING", "entities": "JSON",
                 "rows_read": "LONG", "rows_emitted": "LONG", "rows_quarantined": "LONG", "original_key": "STRING",
                 "started_at": "UTC_DATETIME", "finished_at": "UTC_DATETIME"}},
    {"table": "pf_entity_audit", "primary_key": ["pf_run_id", "package_hash", "entity"],
     "columns": {"pf_run_id": "STRING", "package_hash": "STRING", "entity": "STRING", "package": "STRING", "status": "STRING",
                 "reason": "STRING", "rows_read": "LONG", "rows_emitted": "LONG", "rows_quarantined": "LONG"}},
    {"table": "pf_rule_results", "primary_key": ["pf_run_id", "package_hash", "rule_id", "entity", "attribute"],
     "columns": {"pf_run_id": "STRING", "package_hash": "STRING", "rule_id": "STRING", "entity": "STRING",
                 "attribute": "STRING", "package": "STRING", "scope": "STRING", "kind": "STRING", "mode": "STRING",
                 "action": "STRING", "evaluated": "LONG", "failed": "LONG", "outcome": "STRING"}},
    {"table": "pf_quarantine", "primary_key": ["package_hash", "entity", "member", "row_number", "rule_id"],
     "columns": {"package_hash": "STRING", "entity": "STRING", "member": "STRING", "row_number": "LONG",
                 "rule_id": "STRING", "reason": "STRING", "package": "STRING", "pf_run_id": "STRING"}},
]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def schema(lock: dict) -> list[dict]:
    out = []
    for e in lock["entities"]:
        cols: dict[str, Any] = {}
        for a in e["attributes"]:
            cols[a["name"]] = {"type": "DECIMAL", "precision": a["precision"], "scale": a["scale"]} if a["type"] == "DECIMAL" else a["type"]
        cols.update(LINEAGE)
        pk = list(e["primary_key"]) if e["load"] == "upsert" else ["pf_package_hash", "pf_member", "pf_row"]
        out.append({"table": e["name"], "primary_key": pk, "columns": cols})
    return out + [dict(t) for t in EVIDENCE_TABLES]


# ---------------------------------------------------------------------------- state
def _tup(x: Any) -> Any:
    return tuple(_tup(i) for i in x) if isinstance(x, list) else x


@dataclass
class State:
    packages: dict[str, dict] = field(default_factory=dict)
    floor: tuple | None = None
    cursors: dict[str, Any] = field(default_factory=dict)
    unmatched: dict[str, list] = field(default_factory=dict)  # path -> [size, mtime] already reported

    @classmethod
    def load(cls, raw: dict | None) -> "State":
        raw = raw or {}
        return cls(dict(raw.get("packages", {})), _tup(raw["floor"]) if raw.get("floor") is not None else None,
                   dict(raw.get("cursors", {})), dict(raw.get("unmatched", {})))

    def dump(self) -> dict:
        return {"v": 1, "packages": self.packages, "floor": self.floor, "cursors": self.cursors, "unmatched": self.unmatched}

    def prune(self, keep: int) -> None:
        if len(self.packages) <= keep:
            return
        ordered = sorted(self.packages.items(), key=lambda kv: _tup(kv[1]["order"]))
        drop = ordered[: len(ordered) - keep]
        for path, info in drop:
            del self.packages[path]
            k = _tup(info["order"])
            self.floor = k if self.floor is None or k > self.floor else self.floor


# ---------------------------------------------------------------------------- prepared delivery
@dataclass
class EntityOutput:
    name: str
    spool: str | None = None
    read: int = 0
    emitted: int = 0
    quarantined: list[tuple[str, int, str]] = field(default_factory=list)  # (member, row, rule_id)
    status: str = "loaded"
    reason: str = ""


@dataclass
class Prepared:
    pkg: P.Package
    status: str  # loaded | rejected | failed | skip | config_error
    sha256: str = ""
    fingerprint: str = ""  # sha256 of the delivery + its checksum sidecars: a fixed sidecar makes a rejection retryable
    size: int = 0
    rule_id: str = ""
    reason: str = ""
    original_key: str = ""
    entities: dict[str, EntityOutput] = field(default_factory=dict)
    stats: R.Stats = field(default_factory=R.Stats)
    started_at: str = ""
    finished_at: str = ""
    workdir: str = ""
    error: BaseException | None = None


_EVIDENCE_PK = {t["table"]: t["primary_key"] for t in EVIDENCE_TABLES}


class Sync:
    def __init__(self, lock: dict, configuration: dict, state: dict, op: Any, sdk_log: Any, *,
                 run_id: str | None = None, now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 session_factory: Callable[[dict, dict], Any] | None = None):
        self.lock = lock
        self.cfg = configuration
        self.op = op
        self.log = Log(sdk_log)
        self.state = State.load(state)
        self.run_id = run_id or uuid.uuid4().hex
        self.now = now
        self.sleep = sleep
        rt = lock["runtime"]
        self.retry_kw = dict(attempts=rt["retry"]["attempts"], base=rt["retry"]["base_seconds"], maximum=rt["retry"]["max_seconds"], log=self.log, sleep=sleep)
        self.env = configuration.get("pf_environment", "")
        self._tls = threading.local()
        self._sessions: list[Any] = []
        self._sessions_lock = threading.Lock()
        self.warnings = 0
        self.session_factory = session_factory

    # ------------------------------------------------------------------ helpers
    def _check_configuration(self) -> None:
        if self.env not in self.lock["environments"]:
            raise ConfigError(f"configuration key pf_environment must be one of {self.lock['environments']}")
        missing = [k for k in self.lock["secrets"].get(self.env, []) if not self.cfg.get(k)]
        if missing:
            raise ConfigError(f"configuration keys missing or empty: {missing}")

    def _warn(self, message: str) -> None:
        if self.warnings < 9:  # Fivetran keeps at most 10 warnings per sync; leave room for the summary
            self.op.warning(message)
        self.warnings += 1

    def _session(self):
        s = getattr(self._tls, "session", None)
        if s is None:
            factory = self.session_factory or open_file_source
            s = retry(lambda: factory(self.lock["source"], self.cfg), what="connect", **self.retry_kw)
            self._tls.session = s
            with self._sessions_lock:
                self._sessions.append(s)
        return s

    def _reset_session(self) -> None:
        s = getattr(self._tls, "session", None)
        if s is not None:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
            self._tls.session = None

    def _with_session(self, fn, what: str):
        def attempt():
            try:
                return fn(self._session())
            except Exception:
                self._reset_session()
                raise
        return retry(attempt, what=what, **self.retry_kw)

    # ------------------------------------------------------------------ entry
    def _evidence(self, table: str, data: dict) -> None:
        """Evidence rows: primary-key columns are never null or empty (Fivetran rejects both); '-' marks 'not applicable'."""
        for k in _EVIDENCE_PK[table]:
            if data.get(k) is None or data.get(k) == "":
                data[k] = "-"
        self.op.upsert(table=table, data=data)

    def run(self) -> None:
        self.log.info("sync_start", pipeline=self.lock["pipeline"], version=self.lock["version"], run_id=self.run_id,
                      environment=self.env, runtime=VERSION)
        try:
            self._check_configuration()
            if self.lock["source"]["type"] in ("sftp", "ftp"):
                self._run_files()
            else:
                self._run_rds()
        except ConfigError as exc:
            self.log.error("config_error", error=str(exc))
            self.op.error(f"Configuration error: {exc}")
            return
        except SyncFailed as exc:
            self.log.error("sync_failed", rule_id=exc.rule_id, reason=exc.reason)
            self.op.error(f"Sync stopped by rule {exc.rule_id}: {exc.reason}")
            return
        except TransientError as exc:  # retries exhausted: fail this sync cleanly; the next scheduled sync resumes from state
            self.log.error("source_unavailable", error=str(exc))
            self.op.error(f"Source unavailable after retries: {exc}. Progress is checkpointed; the next sync resumes.")
            return
        finally:
            for s in self._sessions:
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass
        if self.warnings > 9:
            self.op.warning(f"{self.warnings - 9} more warnings; see pf_package_audit for run {self.run_id}")
        self.log.info("sync_end", run_id=self.run_id)

    # ------------------------------------------------------------------ file sources
    def _run_files(self) -> None:
        src = self.lock["source"]
        spec = src["package"]
        files = self._with_session(lambda s: s.list_files(), "list")
        self._listing = {f.path: f for f in files}
        ready, not_ready, ignored = P.discover(files, spec, now=self.now())
        self.log.info("discovered", files=len(files), ready=len(ready), not_ready=not_ready, unmatched=len(ignored))
        self._report_unmatched(ignored, spec.get("on_unmatched", "warn"))
        todo: list[P.Package] = []
        for pkg in ready:
            known = self.state.packages.get(pkg.path)
            if known and known["size"] == pkg.file.size and known["mtime"] == pkg.file.mtime \
                    and (known["status"] != "rejected" or known.get("side") == self._side_meta(pkg)):
                continue  # unchanged since it was processed (a rejected one also needs unchanged sidecars)
            if known is None and self.state.floor is not None and pkg.order_key <= self.state.floor:
                self.log.warning("late_package_skipped", package=pkg.path)
                continue
            todo.append(pkg)
        if not todo:
            self.log.info("nothing_to_do")
            return
        seen_hashes = {i["sha256"] for i in self.state.packages.values() if i.get("status") == "loaded"}
        par = self.lock["runtime"]["parallelism"]
        with ThreadPoolExecutor(max_workers=par, thread_name_prefix="pf") as pool:
            window: deque[tuple[P.Package, Future]] = deque()
            queue = deque(todo)
            while queue or window:
                while queue and len(window) < par + 1:
                    pkg = queue.popleft()
                    window.append((pkg, pool.submit(self._prepare, pkg, seen_hashes)))
                pkg, fut = window.popleft()
                prepared = fut.result()
                if prepared.status == "loaded" and prepared.sha256 in seen_hashes:
                    dup = next((c for c in spec.get("checks", []) if c["kind"] == "duplicate_package"), None)
                    if dup is not None:  # same content twice in one sync: the earlier one won
                        try:
                            R.package_check(dup, prepared.stats, False, "identical content was already loaded")
                        except PackageRejected as exc:
                            prepared.status, prepared.rule_id, prepared.reason = "rejected", exc.rule_id, exc.reason
                        except SyncFailed as exc:
                            prepared.status, prepared.rule_id, prepared.reason = "failed", exc.rule_id, exc.reason
                try:
                    self._emit(prepared)
                finally:
                    if prepared.workdir:
                        shutil.rmtree(prepared.workdir, ignore_errors=True)
                if prepared.status == "loaded":
                    seen_hashes.add(prepared.sha256)
                if prepared.status == "failed":
                    for _, f in window:
                        f.cancel()
                    raise SyncFailed(prepared.rule_id, prepared.reason)
                if prepared.status == "config_error":
                    raise ConfigError(prepared.reason)

    def _report_unmatched(self, ignored: list[str], policy: str) -> None:
        """Files in the drop that match no delivery pattern (misnamed, misplaced): audited once, not silently ignored."""
        if policy == "ignore":
            return
        new = []
        for path in ignored:
            f = self._listing.get(path)
            meta = [f.size, f.mtime] if f else [None, None]
            if self.state.unmatched.get(path) != meta:
                new.append((path, meta))
        for path, meta in new:
            self._evidence("pf_package_audit", {
                "pf_run_id": self.run_id, "package_hash": "-", "package": path, "pipeline": self.lock["pipeline"],
                "pipeline_version": self.lock["version"], "runtime_version": VERSION, "spec_hash": self.lock["spec_sha256"],
                "environment": self.env, "package_size": meta[0] or 0, "status": "unmatched", "rule_id": "package_match",
                "reason": "file matches no delivery pattern (source.package.match)", "entities": {}, "rows_read": 0,
                "rows_emitted": 0, "rows_quarantined": 0, "original_key": "", "started_at": _now(), "finished_at": _now()})
            self.state.unmatched[path] = meta
            self._warn(f"unmatched file in the source: {path}")
        if new:
            if len(self.state.unmatched) > 2000:  # bounded state
                for k in sorted(self.state.unmatched)[: len(self.state.unmatched) - 2000]:
                    del self.state.unmatched[k]
            self.op.checkpoint(state=self.state.dump())

    def _side_meta(self, pkg: P.Package) -> list:
        """Listing metadata of the delivery's sidecars (cheap: no download) — a re-sent sidecar changes it."""
        d = posixpath.dirname(pkg.path)
        out = []
        for side in sorted(P.sidecar_names(self.lock["source"]["package"], posixpath.basename(pkg.path))):
            f = getattr(self, "_listing", {}).get(posixpath.join(d, side) if d else side)
            out.append([side, f.size, f.mtime] if f else [side, None, None])
        return out

    def _fingerprint(self, pkg: P.Package, sha: str) -> str:
        spec = self.lock["source"]["package"]
        name = posixpath.basename(pkg.path)
        d = posixpath.dirname(pkg.path)
        h = hashlib.sha256(sha.encode())
        for side in sorted(P.sidecar_names(spec, name)):
            text = self._with_session(lambda s, r=(posixpath.join(d, side) if d else side): s.read_text(r), "sidecar")
            h.update(b"\0" + side.encode() + b"\0" + (text or "").encode())
        return h.hexdigest()

    def _prepare(self, pkg: P.Package, seen_hashes: set[str]) -> Prepared:
        spec = self.lock["source"]["package"]
        out = Prepared(pkg, "loaded", size=pkg.file.size, started_at=_now())
        workdir = P.new_workdir()
        out.workdir = workdir
        local = os.path.join(workdir, "package.bin")
        try:
            if pkg.file.size > spec["max_package_bytes"]:
                raise PackageRejected("max_package_bytes", "delivery exceeds max_package_bytes")
            self._with_session(lambda s: s.download(pkg.path, local), "download")
            sha, md5 = P.hash_file(local)
            out.sha256 = sha
            lp = P.LocalPackage(pkg, workdir, local, sha, md5)
            out.fingerprint = self._fingerprint(pkg, sha)
            known = self.state.packages.get(pkg.path)
            if known and known.get("status") == "rejected" and known.get("fingerprint", known.get("sha256")) == out.fingerprint:
                out.status, out.reason = "skip", "unchanged since it was rejected"
                return out
            if known and known.get("status") == "loaded" and known.get("sha256") == sha:
                out.status = "skip"
                out.reason = "unchanged content"
                return out
            if known and known.get("status") == "loaded":
                policy = spec.get("on_redelivery", "reject")
                if policy == "skip":
                    out.status, out.reason = "skip", "redelivered with new content; on_redelivery: skip"
                    return out
                if policy == "reject":
                    raise PackageRejected("on_redelivery", "a loaded delivery was resent with different content")
            originals = self.lock.get("originals", {}).get(self.env)
            if self.lock.get("archive_originals") and originals:
                out.original_key = retry(lambda: P.archive_original(lp, originals, self.cfg, self.lock["pipeline"]),
                                         what="archive", **self.retry_kw)
            P.run_receive_checks(lp, spec.get("checks", []), out.stats, _Reader(self, pkg), seen_hashes)
            P.unpack(lp, spec.get("transforms", []))
            P.run_member_checks(lp, spec.get("checks", []), out.stats)
            self._entities(lp, out)
        except PackageRejected as exc:
            out.status, out.rule_id, out.reason = "rejected", exc.rule_id, exc.reason
            for e in out.entities.values():
                e.spool = None
        except SyncFailed as exc:
            out.status, out.rule_id, out.reason = "failed", exc.rule_id, exc.reason
        except ConfigError as exc:
            out.status, out.reason = "config_error", str(exc)
        except Exception as exc:  # unexpected or retries exhausted: fail the sync, never skip silently
            out.status, out.rule_id, out.reason = "failed", "runtime_error", f"{type(exc).__name__} while preparing the delivery"
            out.error = exc
        finally:
            out.finished_at = _now()
        return out

    def _entities(self, lp: P.LocalPackage, out: Prepared) -> None:
        bound: set[str] = set()
        controls = {c["control_member"] for e in self.lock["entities"] for c in e.get("checks", []) if c["kind"] == "control_count"}
        for e in self.lock["entities"]:
            eo = EntityOutput(e["name"])
            out.entities[e["name"]] = eo
            rx = re.compile(e["file"]["member"])
            members = sorted(m for m in lp.members if rx.fullmatch(m))
            bound.update(members)
            try:
                self._entity_rows(e, members, lp, out, eo)
                self._entity_checks(e, lp, out, eo)
                R.escalate_thresholds(e, out.stats, eo.read)
            except EntityRejected as exc:
                eo.status, eo.reason, eo.spool = "rejected", f"{exc.rule_id}: {exc.reason}", None
        unmatched = [m for m in lp.members if m not in bound and not any(re.fullmatch(c, m) for c in controls)]
        if unmatched:
            self.log.warning("unmatched_members", package=lp.pkg.path, count=len(unmatched))

    def _entity_rows(self, e: dict, members: list[str], lp: P.LocalPackage, out: Prepared, eo: EntityOutput) -> None:
        expected = [a.get("source") or a["name"] for a in e["attributes"]]
        fd, spool = tempfile.mkstemp(prefix=f"{e['name']}_", suffix=".jsonl", dir=lp.workdir)
        eo.spool = spool
        with os.fdopen(fd, "w", encoding="utf-8") as sp:
            for m in members:
                rd = Reader(lp.members[m], e["file"]["format"], expected)
                checked_header = False
                for row_no, raw in rd.rows():
                    if not checked_header:
                        self._check_header(e, rd.header)
                        checked_header = True
                    eo.read += 1
                    if raw.get("__pf_malformed__"):
                        rule = {"id": f"malformed__{e['name']}", "kind": "format", "on_fail": "quarantine_row", "mode": "enforce"}
                        st = out.stats.get(rule, "row", e["name"])
                        st.evaluated += 1
                        st.failed += 1
                        st.outcome = "quarantined"
                        eo.quarantined.append((m, row_no, rule["id"]))
                        continue
                    res = R.process_row(raw, e, self.cfg, out.stats)
                    if res.row is None:
                        for rule_id, _ in res.failures:
                            eo.quarantined.append((m, row_no, rule_id))
                        continue
                    rec = {k: to_emit(v) for k, v in res.row.items()}
                    rec.update({"pf_package": lp.pkg.path, "pf_package_hash": lp.sha256, "pf_member": m, "pf_row": row_no,
                                "pf_run_id": self.run_id, "pf_pipeline_version": self.lock["version"]})
                    sp.write(json.dumps(rec, separators=(",", ":")) + "\n")
                    eo.emitted += 1
                if not checked_header and e["file"]["format"].get("type") == "csv":
                    self._check_header(e, rd.header)

    def _check_header(self, e: dict, header: list[str]) -> None:
        fmt = e["file"]["format"]
        if fmt.get("type") != "csv" or not fmt.get("header", True):
            return
        expected = [a.get("source") or a["name"] for a in e["attributes"]]
        if not header:
            return  # empty member: row_count decides
        missing = [c for c in expected if c not in header]
        if missing:
            raise PackageRejected(f"schema__{e['name']}", f"{len(missing)} mapped column(s) missing from the file header")
        extra = [c for c in header if c not in expected]
        if extra and e["file"].get("extra_columns", "reject") == "reject":
            raise PackageRejected(f"schema__{e['name']}", f"{len(extra)} unmapped column(s) in the file header")

    def _entity_checks(self, e: dict, lp: P.LocalPackage, out: Prepared, eo: EntityOutput) -> None:
        for c in e.get("checks", []):
            k = c["kind"]
            if k == "row_count":
                n = eo.emitted
                ok = n >= c.get("min", 1) and (c.get("max") is None or n <= c["max"])
                R.entity_check(c, e["name"], out.stats, ok, f"{n} rows outside the expected count")
            elif k == "unique":
                seen: set[str] = set()
                dupes = 0
                if eo.spool:
                    with open(eo.spool, encoding="utf-8") as fh:
                        for line in fh:
                            rec = json.loads(line)
                            key = hashlib.sha256(json.dumps([rec.get(x) for x in c["columns"]], default=str).encode()).hexdigest()
                            if key in seen:
                                dupes += 1
                            seen.add(key)
                R.entity_check(c, e["name"], out.stats, dupes == 0, f"{dupes} duplicate key(s) within the delivery")
            elif k == "control_count":
                expected = P.control_count(lp, c)
                ok = expected is not None and expected == eo.read
                R.entity_check(c, e["name"], out.stats, ok, "control file missing" if expected is None else "row count differs from the control file")

    # ------------------------------------------------------------------ emission (main thread only)
    def _emit(self, p: Prepared) -> None:
        if p.status == "skip":
            info = self.state.packages.get(p.pkg.path, {})
            if info:
                info.update(size=p.pkg.file.size, mtime=p.pkg.file.mtime)
                self.op.checkpoint(state=self.state.dump())
            self.log.info("package_skipped", package=p.pkg.path, reason=p.reason)
            return
        rows_emitted = rows_q = rows_read = 0
        every = self.lock["runtime"]["checkpoint_every_rows"]
        since_cp = 0
        if p.status == "loaded":
            for eo in p.entities.values():
                rows_read += eo.read
                if eo.status != "loaded" or not eo.spool:
                    continue
                with open(eo.spool, encoding="utf-8") as fh:
                    for line in fh:
                        self.op.upsert(table=eo.name, data=json.loads(line))
                        rows_emitted += 1
                        since_cp += 1
                        if since_cp >= every:
                            self.op.checkpoint(state=self.state.dump())  # package not marked done: a crash re-emits it (idempotent keys)
                            since_cp = 0
                for member, row_no, rule_id in eo.quarantined:
                    self._evidence("pf_quarantine", {
                        "package_hash": p.sha256, "entity": eo.name, "member": member, "row_number": row_no,
                        "rule_id": rule_id, "reason": "check_failed", "package": p.pkg.path, "pf_run_id": self.run_id})
                    rows_q += 1
        entities = {n: {"read": eo.read, "emitted": eo.emitted if (p.status == "loaded" and eo.status == "loaded") else 0,
                        "quarantined": len(eo.quarantined), "status": eo.status if p.status == "loaded" else p.status, "reason": eo.reason}
                    for n, eo in p.entities.items()}
        for n, ed in entities.items():
            self._evidence("pf_entity_audit", {
                "pf_run_id": self.run_id, "package_hash": p.sha256 or "-", "entity": n, "package": p.pkg.path,
                "status": ed["status"], "reason": ed["reason"] or p.reason, "rows_read": ed["read"], "rows_emitted": ed["emitted"],
                "rows_quarantined": ed["quarantined"]})
        for (rule_id, ent, attr), st in sorted(p.stats.rules.items()):
            self._evidence("pf_rule_results", {
                "pf_run_id": self.run_id, "package_hash": p.sha256 or "-", "rule_id": rule_id, "entity": ent, "attribute": attr,
                "package": p.pkg.path, "scope": st.scope, "kind": st.kind, "mode": st.mode, "action": st.action,
                "evaluated": st.evaluated, "failed": st.failed, "outcome": st.outcome})
        self._evidence("pf_package_audit", {
            "pf_run_id": self.run_id, "package_hash": p.sha256 or "-", "package": p.pkg.path, "pipeline": self.lock["pipeline"],
            "pipeline_version": self.lock["version"], "runtime_version": VERSION, "spec_hash": self.lock["spec_sha256"],
            "environment": self.env, "package_size": p.size, "status": p.status, "rule_id": p.rule_id, "reason": p.reason,
            "entities": entities, "rows_read": rows_read, "rows_emitted": rows_emitted, "rows_quarantined": rows_q,
            "original_key": p.original_key, "started_at": p.started_at, "finished_at": p.finished_at})
        if p.status in ("loaded", "rejected"):
            self.state.packages[p.pkg.path] = {"sha256": p.sha256, "fingerprint": p.fingerprint, "size": p.pkg.file.size,
                                               "mtime": p.pkg.file.mtime, "status": p.status, "order": list(p.pkg.order_key)}
            if p.status == "rejected":
                self.state.packages[p.pkg.path]["side"] = self._side_meta(p.pkg)
            lookback = max([c.get("lookback_packages", 500) for c in self.lock["source"]["package"].get("checks", []) if c["kind"] == "duplicate_package"] or [2000])
            self.state.prune(max(lookback, 50))
        self.op.checkpoint(state=self.state.dump())
        if p.status == "rejected":
            self._warn(f"Delivery {p.pkg.path} rejected by {p.rule_id}: {p.reason}")
        self.log.info("package_done", package=p.pkg.path, status=p.status, rule_id=p.rule_id, emitted=rows_emitted, quarantined=rows_q)

    # ------------------------------------------------------------------ rds
    def _run_rds(self) -> None:
        from .sources.rds import RdsSession

        src = self.lock["source"]
        factory = self.session_factory or RdsSession
        session = retry(lambda: factory(src, self.cfg), what="connect", **self.retry_kw)
        try:
            for e in self.lock["entities"]:
                self._rds_entity(session, e)
        finally:
            session.close()

    def _rds_entity(self, session, e: dict) -> None:
        tb = e["table"]
        started = _now()
        stats = R.Stats()
        cols = [a.get("source") or a["name"] for a in e["attributes"]]
        available = retry(lambda: session.columns(tb["table"]), what="columns", **self.retry_kw)
        missing = [c for c in cols if c not in available]
        batch_id = hashlib.sha256(f"{self.run_id}|{e['name']}".encode()).hexdigest()
        if missing:
            self._rds_audit(e, batch_id, started, "failed", f"schema__{e['name']}", f"{len(missing)} mapped column(s) missing in {tb['table']}", 0, 0, 0, stats)
            self.op.checkpoint(state=self.state.dump())
            raise SyncFailed(f"schema__{e['name']}", f"{len(missing)} mapped column(s) missing in {tb['table']}")
        inc = tb.get("incremental")
        cursor_col = inc["cursor_column"] if inc else None
        since = self.state.cursors.get(e["name"], inc.get("initial_value") if inc else None)
        incremental_run = bool(cursor_col) and e["name"] in self.state.cursors  # not the initial full load
        if not inc:
            self.op.truncate(table=e["name"])  # full reload: rows absent from the source become soft-deleted
        read = emitted = quarantined = 0
        pk_sources = [next(a.get("source") or a["name"] for a in e["attributes"] if a["name"] == k) for k in e["primary_key"]]
        order_extra = [c for c in pk_sources if c != cursor_col]
        try:
            for batch in session.read(tb["table"], cols, cursor_col, since, order_extra, self.lock["source"]["batch_size"]):
                last_cursor = None
                for raw in batch:
                    read += 1
                    res = R.process_row(raw, e, self.cfg, stats)
                    if cursor_col and raw.get(cursor_col) is not None:
                        last_cursor = raw[cursor_col]
                    if res.row is None:
                        for rule_id, _ in res.failures:
                            self._evidence("pf_quarantine", {
                                "package_hash": batch_id, "entity": e["name"], "member": tb["table"], "row_number": read,
                                "rule_id": rule_id, "reason": "check_failed", "package": f"rds:{tb['table']}", "pf_run_id": self.run_id})
                        quarantined += 1
                        continue
                    rec = {k: to_emit(v) for k, v in res.row.items()}
                    rec.update({"pf_package": f"rds:{tb['table']}", "pf_package_hash": batch_id, "pf_member": tb["table"],
                                "pf_row": read, "pf_run_id": self.run_id, "pf_pipeline_version": self.lock["version"]})
                    self.op.upsert(table=e["name"], data=rec)
                    emitted += 1
                if last_cursor is not None:
                    self.state.cursors[e["name"]] = to_emit(last_cursor)
                self.op.checkpoint(state=self.state.dump())
        except (PackageRejected, EntityRejected) as exc:
            # rows already emitted cannot be withdrawn for a database read: stop the sync instead
            self._rds_audit(e, batch_id, started, "failed", exc.rule_id, exc.reason, read, emitted, quarantined, stats)
            self.op.checkpoint(state=self.state.dump())
            raise SyncFailed(exc.rule_id, exc.reason) from None
        status, rule_id, reason = "loaded", "", ""
        for c in e.get("checks", []):
            try:
                if c["kind"] == "row_count":
                    # an incremental read returns only changed rows, often none: `min` applies to the initial load only
                    low_ok = incremental_run or emitted >= c.get("min", 1)
                    ok = low_ok and (c.get("max") is None or emitted <= c["max"])
                    R.entity_check(c, e["name"], stats, ok, f"{emitted} rows outside the expected count")
            except (PackageRejected, EntityRejected, SyncFailed) as exc:
                status, rule_id, reason = "failed", exc.rule_id, exc.reason
        self._rds_audit(e, batch_id, started, status, rule_id, reason, read, emitted, quarantined, stats)
        self.op.checkpoint(state=self.state.dump())
        if status == "failed":
            raise SyncFailed(rule_id, reason)

    def _rds_audit(self, e, batch_id, started, status, rule_id, reason, read, emitted, quarantined, stats: R.Stats) -> None:
        self._evidence("pf_entity_audit", {
            "pf_run_id": self.run_id, "package_hash": batch_id, "entity": e["name"], "package": f"rds:{e['table']['table']}",
            "status": status, "reason": reason, "rows_read": read, "rows_emitted": emitted, "rows_quarantined": quarantined})
        for (rid, ent, attr), st in sorted(stats.rules.items()):
            self._evidence("pf_rule_results", {
                "pf_run_id": self.run_id, "package_hash": batch_id, "rule_id": rid, "entity": ent, "attribute": attr,
                "package": f"rds:{e['table']['table']}", "scope": st.scope, "kind": st.kind, "mode": st.mode, "action": st.action,
                "evaluated": st.evaluated, "failed": st.failed, "outcome": st.outcome})
        self._evidence("pf_package_audit", {
            "pf_run_id": self.run_id, "package_hash": batch_id, "package": f"rds:{e['table']['table']}",
            "pipeline": self.lock["pipeline"], "pipeline_version": self.lock["version"], "runtime_version": VERSION,
            "spec_hash": self.lock["spec_sha256"], "environment": self.env, "package_size": 0, "status": status,
            "rule_id": rule_id, "reason": reason, "entities": {e["name"]: {"read": read, "emitted": emitted, "quarantined": quarantined, "status": status}},
            "rows_read": read, "rows_emitted": emitted, "rows_quarantined": quarantined, "original_key": "",
            "started_at": started, "finished_at": _now()})


class _Reader:
    """Lets structural checks read sidecar files through the worker's own session."""

    def __init__(self, sync: Sync, pkg: P.Package):
        self.sync = sync

    def read_text(self, rel: str):
        return self.sync._with_session(lambda s: s.read_text(rel), "sidecar")


def update(lock: dict, configuration: dict, state: dict, op: Any, log: Any, **kw) -> None:
    Sync(lock, configuration, state, op, log, **kw).run()
