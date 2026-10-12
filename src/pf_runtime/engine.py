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


STATE_VERSION = 2
MAX_PACKAGE_RECORDS = 20000   # ~5 MB of state; Fivetran's limit is 10 MB
MAX_LOADED_HASHES = 20000


@dataclass
class State:
    """Connector state. packages[path]: the last processed version of a delivery path (sha256, fingerprint, size,
    mtime, status, order) and, separately, `loaded_sha256` — what was actually loaded there, which a later
    rejection never overwrites. loaded_hashes: recent loaded content hashes (duplicate detection). max_loaded:
    highest order key ever loaded (late-delivery detection). floor: highest order key pruned from state."""

    packages: dict[str, dict] = field(default_factory=dict)
    floor: tuple | None = None
    cursors: dict[str, Any] = field(default_factory=dict)
    unmatched: dict[str, list] = field(default_factory=dict)
    max_loaded: tuple | None = None
    loaded_hashes: list[str] = field(default_factory=list)
    fresh: bool = False  # empty state: first sync or a Fivetran historical re-sync

    @classmethod
    def load(cls, raw: dict | None) -> "State":
        raw = raw or {}
        try:
            if not isinstance(raw, dict):
                raise TypeError("state is not an object")
            pk = raw.get("packages", {})
            if not isinstance(pk, dict) or any(not isinstance(v, dict) or "status" not in v or "order" not in v for v in pk.values()):
                raise TypeError("packages")
            st = cls(dict(pk), _tup(raw["floor"]) if raw.get("floor") is not None else None, dict(raw.get("cursors", {})),
                     dict(raw.get("unmatched", {})), _tup(raw["max_loaded"]) if raw.get("max_loaded") is not None else None,
                     list(raw.get("loaded_hashes", [])), fresh=not raw)
            if raw.get("v", 1) < 2:  # v1: a loaded record's sha256 is what was loaded
                for v in st.packages.values():
                    if v.get("status") == "loaded":
                        v["loaded_sha256"] = v["sha256"][:32]
                        st.max_loaded = max(st.max_loaded, _tup(v["order"])) if st.max_loaded is not None else _tup(v["order"])
                        st.loaded_hashes.append(v["sha256"])
            if st.floor is not None and not isinstance(st.floor, tuple):
                raise TypeError("floor")
            if st.max_loaded is not None and not isinstance(st.max_loaded, tuple):
                raise TypeError("max_loaded")
            for v in st.packages.values():
                if not isinstance(v["order"], list):
                    raise TypeError("order")
            return st
        except (TypeError, KeyError, ValueError, AttributeError) as exc:
            raise ConfigError(f"connector state is not compatible with runtime {VERSION} ({exc}); "
                              "run a historical re-sync to rebuild it from the originals registry") from None

    def dump(self) -> dict:
        return {"v": STATE_VERSION, "packages": self.packages, "floor": self.floor, "cursors": self.cursors,
                "unmatched": self.unmatched, "max_loaded": self.max_loaded, "loaded_hashes": self.loaded_hashes}

    def record_loaded(self, sha: str, order: tuple) -> None:
        sha = sha[:32]  # 128 bits are plenty for duplicate detection and halve the state size
        if sha not in self.loaded_hashes:
            self.loaded_hashes.append(sha)
            del self.loaded_hashes[:-MAX_LOADED_HASHES]
        self.max_loaded = order if self.max_loaded is None or order > self.max_loaded else self.max_loaded

    def prune(self, keep: int) -> None:
        if len(self.packages) <= keep:
            return
        ordered = sorted(self.packages.items(), key=lambda kv: _tup(kv[1]["order"]))
        for path, info in ordered[: len(ordered) - keep]:
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
    late: bool = False


_EVIDENCE_PK = {t["table"]: t["primary_key"] for t in EVIDENCE_TABLES}


class Sync:
    def __init__(self, lock: dict, configuration: dict, state: dict, op: Any, sdk_log: Any, *,
                 run_id: str | None = None, now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 session_factory: Callable[[dict, dict], Any] | None = None):
        self.lock = lock
        self.cfg = configuration
        self.op = op
        self.log = Log(sdk_log)
        self._raw_state = state
        self.state = State()
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
        self._last_cp = time.monotonic()
        self._listing: dict[str, Any] = {}

    def _checkpoint(self, force: bool = False) -> None:
        """Fivetran: checkpoint about every 10 minutes and never more than once a minute. Deliveries are recorded in
        state as they finish but committed at most once a minute and always at the end (re-emission after a crash
        is idempotent: every row key is deterministic)."""
        if force or time.monotonic() - self._last_cp >= 60:
            self.op.checkpoint(state=self.state.dump())
            self._last_cp = time.monotonic()

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
            self.state = State.load(self._raw_state)
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
            self.op.error(f"Source unavailable after retries: {exc}. Completed deliveries and incremental tables resume "
                          "from the last checkpoint; a full-reload table is re-read next sync.")
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
        try:
            self._run_files_inner()
        finally:
            self._checkpoint(force=True)

    def _audit_skip(self, path: str, size: int, status: str, rule_id: str, reason: str) -> None:
        self._evidence("pf_package_audit", {
            "pf_run_id": self.run_id, "package_hash": "-", "package": path, "pipeline": self.lock["pipeline"],
            "pipeline_version": self.lock["version"], "runtime_version": VERSION, "spec_hash": self.lock["spec_sha256"],
            "environment": self.env, "package_size": size or 0, "status": status, "rule_id": rule_id, "reason": reason,
            "entities": {}, "rows_read": 0, "rows_emitted": 0, "rows_quarantined": 0, "original_key": "",
            "started_at": _now(), "finished_at": _now()})
        self._warn(f"{status} delivery {path}: {reason}")

    def _rebuild_registry(self) -> None:
        """Empty state with existing originals = a Fivetran historical re-sync (or a lost state). Rebuild which
        deliveries were already loaded from the write-once markers, so redelivery and duplicate rules still hold.
        Configuration pf_full_reload=true deliberately reloads everything (e.g. after a primary-key change)."""
        originals = self.lock.get("originals", {}).get(self.env)
        if not (self.lock.get("archive_originals") and originals) or str(self.cfg.get("pf_full_reload", "")).lower() == "true":
            return
        marks = retry(lambda: P.list_loaded(originals, self.cfg, self.lock["pipeline"]), what="registry", **self.retry_kw)
        for m in marks:
            order = _tup(m["order"])
            self.state.packages.setdefault(m["path"], {"sha256": m["sha256"], "fingerprint": "", "size": None, "mtime": None,
                                                       "status": "loaded", "order": list(order), "loaded_sha256": m["sha256"][:32]})
            self.state.record_loaded(m["sha256"], order)
        if marks:
            self.log.info("registry_rebuilt", loaded=len(marks))
            self._warn(f"empty state: rebuilt {len(marks)} loaded deliveries from the originals registry (re-sync)")

    def _run_files_inner(self) -> None:
        src = self.lock["source"]
        spec = src["package"]
        if self.state.fresh:
            self._rebuild_registry()
        files = self._with_session(lambda s: s.list_files(), "list")
        self._listing = {f.path: f for f in files}
        now = self.now()
        ready, waiting, ignored = P.discover(files, spec, now=now)
        self.log.info("discovered", files=len(files), ready=len(ready), not_ready=len(waiting), unmatched=len(ignored))
        self._report_unmatched(ignored, spec.get("on_unmatched", "warn"))
        for w in waiting:
            if w.file.mtime > now + 300 and self.state.unmatched.get(w.path) != ["future", w.file.mtime]:
                self.state.unmatched[w.path] = ["future", w.file.mtime]
                self._audit_skip(w.path, w.file.size, "waiting", "readiness", "modification time is in the future (clock skew?)")
        policy = self.lock.get("late_data_policy", "wait")
        hold = min((w.order_key for w in waiting), default=None) if policy == "wait" else None
        todo: list[P.Package] = []
        for pkg in ready:
            if hold is not None and pkg.order_key > hold:
                self.log.info("held_back", package=pkg.path, reason="an older delivery is not ready yet (late_data_policy: wait)")
                continue
            known = self.state.packages.get(pkg.path)
            if known and spec.get("change_detection", "metadata") == "metadata" and known.get("size") == pkg.file.size \
                    and known.get("mtime") == pkg.file.mtime \
                    and (known["status"] != "rejected" or known.get("side") == self._side_meta(pkg)):
                continue  # unchanged since it was processed (a rejected one also needs unchanged sidecars)
            if known is None and self.state.floor is not None and pkg.order_key <= self.state.floor:
                self.state.packages[pkg.path] = {"sha256": "", "fingerprint": "", "size": pkg.file.size, "mtime": pkg.file.mtime,
                                                 "status": "late", "order": list(pkg.order_key)}
                self._audit_skip(pkg.path, pkg.file.size, "late", "late_data",
                                 "older than the deliveries kept in state; not loaded (check whether it is new)")
                continue
            if known and known.get("status") == "late" and known.get("size") == pkg.file.size and known.get("mtime") == pkg.file.mtime:
                continue
            todo.append(pkg)
        if not todo:
            self.log.info("nothing_to_do")
            return
        seen_hashes = set(self.state.loaded_hashes)  # 32-hex prefixes
        par = self.lock["runtime"]["parallelism"]
        with ThreadPoolExecutor(max_workers=par, thread_name_prefix="pf") as pool:
            window: deque[tuple[P.Package, Future]] = deque()
            queue = deque(todo)
            while queue or window:
                while queue and len(window) < par + 1:
                    pkg = queue.popleft()
                    window.append((pkg, pool.submit(self._prepare, pkg, frozenset(seen_hashes))))
                pkg, fut = window.popleft()
                prepared = fut.result()
                if prepared.status == "loaded" and prepared.sha256[:32] in seen_hashes:
                    dup = next((c for c in spec.get("checks", []) if c["kind"] == "duplicate_package"), None)
                    if dup is not None:  # same content twice in one sync: the earlier one won
                        try:
                            R.package_check(dup, prepared.stats, False, "identical content was already loaded")
                        except PackageRejected as exc:
                            prepared.status, prepared.rule_id, prepared.reason = "rejected", exc.rule_id, exc.reason
                        except SyncFailed as exc:
                            prepared.status, prepared.rule_id, prepared.reason = "failed", exc.rule_id, exc.reason
                if prepared.status == "loaded" and self.state.max_loaded is not None and pkg.order_key < self.state.max_loaded \
                        and self.state.packages.get(pkg.path, {}).get("loaded_sha256") is None:
                    prepared.late = True  # older than something already loaded
                    if policy in ("wait", "reject_late"):
                        prepared.status, prepared.rule_id = "rejected", "late_data"
                        prepared.reason = f"arrived after newer deliveries were loaded (late_data_policy: {policy})"
                try:
                    self._emit(prepared)
                finally:
                    if prepared.workdir:
                        shutil.rmtree(prepared.workdir, ignore_errors=True)
                if prepared.status == "loaded":
                    seen_hashes.add(prepared.sha256[:32])
                if prepared.status == "failed":
                    for _, f in window:
                        f.cancel()
                    raise SyncFailed(prepared.rule_id, prepared.reason)
                if prepared.status == "config_error":
                    raise ConfigError(prepared.reason)
                if prepared.status == "transient":
                    for _, f in window:
                        f.cancel()
                    raise TransientError(prepared.reason)

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
        if new and len(self.state.unmatched) > 2000:  # bounded state
            for k in sorted(self.state.unmatched)[: len(self.state.unmatched) - 2000]:
                del self.state.unmatched[k]

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

    def _download(self, pkg: P.Package, local: str, limit: int) -> None:
        def attempt(session):
            session.download(pkg.path, local)
            got = os.path.getsize(local)
            if got > limit:
                raise PackageRejected("max_package_bytes", "delivery exceeds max_package_bytes")
            if got != pkg.file.size:  # short or grown transfer: never load a partial file
                raise TransientError(f"downloaded {got} bytes but the listing says {pkg.file.size}")
        self._with_session(attempt, "download")

    def _prepare(self, pkg: P.Package, seen_hashes: frozenset[str]) -> Prepared:
        spec = self.lock["source"]["package"]
        out = Prepared(pkg, "loaded", size=pkg.file.size, started_at=_now())
        workdir = P.new_workdir()
        out.workdir = workdir
        local = os.path.join(workdir, "package.bin")
        try:
            if pkg.file.size > spec["max_package_bytes"]:
                raise PackageRejected("max_package_bytes", "delivery exceeds max_package_bytes")
            self._download(pkg, local, spec["max_package_bytes"])
            sha, md5 = P.hash_file(local)
            out.sha256 = sha
            lp = P.LocalPackage(pkg, workdir, local, sha, md5)
            out.fingerprint = self._fingerprint(pkg, sha)
            known = self.state.packages.get(pkg.path) or {}
            loaded = known.get("loaded_sha256")
            if loaded and loaded == sha[:len(loaded)]:
                out.status, out.reason = "skip", "unchanged content (already loaded)"
                return out
            if known.get("status") in ("rejected", "late") and known.get("fingerprint") == out.fingerprint:
                out.status, out.reason = "skip", "unchanged since it was rejected"
                return out
            if loaded:  # this path was loaded before with different content
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
            P.run_receive_checks(lp, spec.get("checks", []), out.stats, _Reader(self, pkg), set(seen_hashes))
            try:
                P.unpack(lp, spec.get("transforms", []))
                P.run_member_checks(lp, spec.get("checks", []), out.stats)
                self._entities(lp, out)
            except (UnicodeError, EOFError, ValueError) as exc:  # unreadable content is the delivery's fault
                if isinstance(exc, LookupError):
                    raise
                raise PackageRejected("unreadable", f"delivery content could not be read ({type(exc).__name__})") from None
        except PackageRejected as exc:
            out.status, out.rule_id, out.reason = "rejected", exc.rule_id, exc.reason
            for e in out.entities.values():
                e.spool = None
        except SyncFailed as exc:
            out.status, out.rule_id, out.reason = "failed", exc.rule_id, exc.reason
        except ConfigError as exc:
            out.status, out.reason = "config_error", str(exc)
        except TransientError as exc:
            out.status, out.reason = "transient", str(exc)
        except LookupError as exc:  # e.g. an unknown encoding name in the spec
            out.status, out.reason = "config_error", f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # unexpected or retries exhausted: fail the sync, never skip silently
            from .retry import is_transient

            if is_transient(exc):
                out.status, out.reason = "transient", f"{type(exc).__name__} while preparing {pkg.path}"
            else:
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
                if e["file"]["format"].get("type") in ("json", "jsonl") and eo.read:
                    self._check_header(e, rd.header)  # keys seen across the whole member

    def _check_header(self, e: dict, header: list[str]) -> None:
        """CSV: the header row. JSON/JSONL: the union of keys in the member. A mapped column that never appears is a
        schema change (reject); unmapped ones follow extra_columns."""
        fmt = e["file"]["format"]
        if fmt.get("type", "csv") == "csv" and not fmt.get("header", True):
            return
        expected = [a.get("source") or a["name"] for a in e["attributes"]]
        if not header:
            return  # empty member: row_count decides
        missing = [c for c in expected if c not in header]
        if missing:
            raise PackageRejected(f"schema__{e['name']}", f"{len(missing)} mapped column(s) missing from the member")
        extra = [c for c in header if c not in expected]
        if extra and e["file"].get("extra_columns", "reject") == "reject":
            raise PackageRejected(f"schema__{e['name']}", f"{len(extra)} unmapped column(s) in the member")

    def _entity_checks(self, e: dict, lp: P.LocalPackage, out: Prepared, eo: EntityOutput) -> None:
        for c in e.get("checks", []):
            k = c["kind"]
            if k == "row_count":
                n = eo.emitted
                ok = n >= c.get("min", 1) and (c.get("max") is None or n <= c["max"])
                R.entity_check(c, e["name"], out.stats, ok, f"{n} rows outside the expected count")
            elif k == "unique":
                seen: set[bytes] = set()  # 16-byte digests: ~70 B/row instead of ~130 (4 GB container limit)
                dupes = 0
                if eo.spool:
                    with open(eo.spool, encoding="utf-8") as fh:
                        for line in fh:
                            rec = json.loads(line)
                            key = hashlib.blake2b(json.dumps([rec.get(x) for x in c["columns"]], default=str).encode(), digest_size=16).digest()
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
                if p.reason.startswith("unchanged content"):
                    info.update(sha256=p.sha256, fingerprint=p.fingerprint, status="loaded")
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
                            self._checkpoint()  # package not marked done yet: a crash re-emits it (idempotent keys)
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
            "environment": self.env, "package_size": p.size, "status": p.status, "rule_id": p.rule_id,
            "reason": p.reason or ("late: arrived after newer deliveries" if p.late else ""),
            "entities": entities, "rows_read": rows_read, "rows_emitted": rows_emitted, "rows_quarantined": rows_q,
            "original_key": p.original_key, "started_at": p.started_at, "finished_at": p.finished_at})
        if p.status in ("loaded", "rejected"):
            prev = self.state.packages.get(p.pkg.path, {})
            rec = {"sha256": p.sha256, "size": p.pkg.file.size, "mtime": p.pkg.file.mtime,
                   "status": p.status, "order": list(p.pkg.order_key)}
            if p.status != "loaded":
                rec["fingerprint"] = p.fingerprint  # only rejections need it (sidecar fixes)
            if prev.get("loaded_sha256"):
                rec["loaded_sha256"] = prev["loaded_sha256"]  # a rejection never erases what was loaded at this path
            if p.status == "loaded":
                rec["loaded_sha256"] = p.sha256[:32]
                self.state.record_loaded(p.sha256, p.pkg.order_key)
                originals = self.lock.get("originals", {}).get(self.env)
                if self.lock.get("archive_originals") and originals:
                    retry(lambda: P.mark_loaded(originals, self.cfg, self.lock["pipeline"], p.sha256, p.pkg.path, list(p.pkg.order_key)),
                          what="registry", **self.retry_kw)
            else:
                rec["side"] = self._side_meta(p.pkg)
            self.state.packages[p.pkg.path] = rec
            self.state.prune(MAX_PACKAGE_RECORDS)
        self._checkpoint(force=p.status == "failed")
        if p.status == "rejected":
            self._warn(f"Delivery {p.pkg.path} rejected by {p.rule_id}: {p.reason}")
        elif p.late:
            self._warn(f"Late delivery {p.pkg.path} loaded after newer ones (late_data_policy: load_late)")
        self.log.info("package_done", package=p.pkg.path, status=p.status, rule_id=p.rule_id, emitted=rows_emitted, quarantined=rows_q)

    # ------------------------------------------------------------------ rds
    def _run_rds(self) -> None:
        import importlib

        src = self.lock["source"]
        factory = self.session_factory or importlib.import_module(f"{__package__}.sources.rds").RdsSession
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
            # Full reload: rows absent from the source become soft-deleted. Truncate is committed only with the
            # next checkpoint, so nothing is checkpointed until the whole table has been read and checked: a
            # failure part-way leaves the previous snapshot intact.
            self.op.truncate(table=e["name"])
        read = emitted = quarantined = 0
        pk_sources = [next(a.get("source") or a["name"] for a in e["attributes"] if a["name"] == k) for k in e["primary_key"]]
        order_extra = [c for c in pk_sources if c != cursor_col]
        try:
            batches = session.read(tb["table"], cols, cursor_col, since, order_extra, self.lock["source"]["batch_size"])
            while True:
                try:
                    batch = next(batches)
                except StopIteration:
                    break
                except (PackageRejected, EntityRejected, SyncFailed, ConfigError, TransientError):
                    raise
                except Exception as exc:  # driver errors mid-read (connection loss, failover, timeouts)
                    raise TransientError(f"database read failed ({type(exc).__name__})") from None
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
                if inc:
                    if last_cursor is not None:
                        self.state.cursors[e["name"]] = to_emit(last_cursor)
                    self._checkpoint()  # incremental: progress is safe to commit (coalesced to once a minute)
            R.escalate_thresholds(e, stats, read)  # max_failed_pct applies to database reads too
        except (PackageRejected, EntityRejected) as exc:
            # rows already emitted cannot be withdrawn for a database read: stop the sync instead
            self._rds_audit(e, batch_id, started, "failed", exc.rule_id, exc.reason, read, emitted, quarantined, stats)
            if inc:
                self._checkpoint(force=True)
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
        if status == "failed":
            if inc:
                self._checkpoint(force=True)
            raise SyncFailed(rule_id, reason)
        self._checkpoint(force=True)

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
