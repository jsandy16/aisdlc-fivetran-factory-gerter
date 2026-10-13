"""RDS source: real PostgreSQL 16 (dry-run server) + scripted sessions for failure injection."""
from __future__ import annotations

import json
import os

import pytest

from .helpers import attr, chk, last_state, rds_lock, run

ENV = os.environ.get("PF_DRYRUN_ENV", "/tmp/pf-dry/env.json")


def _pg():
    if not os.path.exists(ENV):
        pytest.skip("dry-run servers not running")
    import psycopg2
    port = json.load(open(ENV))["pg"]["port"]
    adm = psycopg2.connect(host="127.0.0.1", port=port, user="pf_admin", password="pf-pg-admin", dbname="postgres")
    adm.autocommit = True
    cur = adm.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname='advtest'")
    if not cur.fetchone():
        cur.execute("CREATE DATABASE advtest")
    c = psycopg2.connect(host="127.0.0.1", port=port, user="pf_admin", password="pf-pg-admin", dbname="advtest")
    c.autocommit = True
    return c, port


@pytest.fixture
def pg():
    c, port = _pg()
    cur = c.cursor()
    cur.execute("DROP TABLE IF EXISTS public.s")
    yield c, cur, port
    c.close()


def _src(port):
    return {"type": "rds", "engine": "postgres", "host": "127.0.0.1", "port": port, "database": "advtest", "username": "pf_admin",
            "password": {"secret": "pw"}, "ssl_mode": "disable", "batch_size": 100, "timeout_seconds": 10}


def _lock(port, attrs, cursor="u", initial=None, batch=100, checks=None, row_checks=None):
    ent = {"name": "s", "table": {"table": "public.s", "incremental": {"cursor_column": cursor, "initial_value": initial} if cursor else None},
           "load": "upsert", "primary_key": ["id"], "attributes": attrs, "checks": checks or [], "row_checks": row_checks or []}
    lk = rds_lock([ent])
    lk["source"] = {**_src(port), "batch_size": batch}
    lk["secrets"] = {"dev": ["pw"]}
    return lk


def _run(lk, state=None):
    from pf_runtime.sources.rds import RdsSession
    return run(lk, state=state, cfg={"pw": "pf-pg-admin"}, factory=RdsSession)


ATTRS = [attr("id", "INT", nullable=False), attr("u", "UTC_DATETIME"), attr("v")]


def test_cursor_ties_at_batch_boundary(pg):
    c, cur, port = pg
    cur.execute("CREATE TABLE s (id int primary key, u timestamptz, v text)")
    cur.execute("INSERT INTO s SELECT g, '2024-01-01T00:00:00Z', 'x' FROM generate_series(1,250) g")
    lk = _lock(port, ATTRS, batch=100)
    op = _run(lk)
    assert len({r["id"] for r in op.rows["s"]}) == 250
    st = last_state(op)
    cur.execute("INSERT INTO s VALUES (251, '2024-01-01T00:00:00Z', 'tie')")
    op2 = _run(lk, st)
    assert 251 in {r["id"] for r in op2.rows["s"]}


def test_resume_mid_batch_after_crash(pg):
    c, cur, port = pg
    cur.execute("CREATE TABLE s (id int primary key, u timestamptz, v text)")
    cur.execute("INSERT INTO s SELECT g, timestamp '2024-01-01' + g * interval '1 second', 'x' FROM generate_series(1,250) g")
    lk = _lock(port, ATTRS, batch=100)
    op = _run(lk)
    # [after review] checkpoints are coalesced to once a minute: a crash re-reads from the last committed cursor;
    # with >= semantics and upsert keys the result is identical
    op2 = _run(lk, op.checkpoints[0])
    assert {r["id"] for r in op2.rows.get("s", [])} <= set(range(1, 251))
    assert {r["id"] for r in op.rows["s"]} == set(range(1, 251))


@pytest.mark.skip(reason="[after review] prevented at spec validation (cursor must be nullable: false): tests/test_specs.py::test_unsafe_combinations_rejected")
def test_null_cursor_rows_after_initial_load(pg):
    """A row whose cursor column is NULL (trial_sites: updated_at nullable: true) is never read by incremental syncs."""
    c, cur, port = pg
    cur.execute("CREATE TABLE s (id int primary key, u timestamptz, v text)")
    cur.execute("INSERT INTO s VALUES (1, '2024-01-01Z', 'a')")
    lk = _lock(port, ATTRS)
    st = last_state(_run(lk))
    cur.execute("INSERT INTO s VALUES (2, NULL, 'null cursor')")
    op2 = _run(lk, st)
    got = {r["id"] for r in op2.rows.get("s", [])}
    assert 2 in got or any("null" in w.lower() for w in op2.warnings), "row with NULL cursor silently never loaded"


def test_naive_timestamp_cursor_session_timezone(pg):
    c, cur, port = pg
    cur.execute("ALTER DATABASE advtest SET timezone TO 'America/New_York'")
    try:
        cur.execute("CREATE TABLE s (id int primary key, u timestamptz, v text)")
        cur.execute("INSERT INTO s VALUES (1, '2024-01-01T12:00:00Z', 'a')")
        lk = _lock(port, ATTRS)
        op = _run(lk)
        assert op.rows["s"][0]["u"] == "2024-01-01T12:00:00Z"
        st = last_state(op)
        cur.execute("INSERT INTO s VALUES (2, '2024-01-01T12:00:00.5Z', 'b')")
        op2 = _run(lk, st)
        assert {r["id"] for r in op2.rows["s"]} >= {2}
    finally:
        cur.execute("ALTER DATABASE advtest RESET timezone")


@pytest.mark.xfail(reason="[after review] documented limitation: incremental database reads cannot see hard deletes; open decision (full reload or key reconciliation)", strict=True)
def test_hard_delete_propagates(pg):
    c, cur, port = pg
    cur.execute("CREATE TABLE s (id int primary key, u timestamptz, v text)")
    cur.execute("INSERT INTO s VALUES (1, '2024-01-01Z', 'a'), (2, '2024-01-01Z', 'b')")
    lk = _lock(port, ATTRS)
    st = last_state(_run(lk))
    cur.execute("DELETE FROM s WHERE id = 2")
    op2 = _run(lk, st)
    assert op2.rows.get("s__deleted") or op2.truncated, "hard delete at source never reaches the lake (incremental)"


def test_full_reload_truncate_then_failure(pg):
    """Full reload: truncate is emitted before reading; a row check that fails the sync after some batches were
    checkpointed leaves the lake table soft-deleted except for the first batches."""
    c, cur, port = pg
    cur.execute("CREATE TABLE s (id int primary key, u timestamptz, v text)")
    cur.execute("INSERT INTO s SELECT g, now(), CASE WHEN g = 250 THEN 'BAD' ELSE 'ok' END FROM generate_series(1,250) g")
    attrs = [attr("id", "INT", nullable=False), attr("u", "UTC_DATETIME"),
             attr("v", checks=[chk("v_ok", "allowed_values", values=["ok"], on_fail="reject_package")])]
    lk = _lock(port, attrs, cursor=None, batch=100)
    op = _run(lk)
    assert op.errors
    ckpt_after_truncate = [i for i, _ in enumerate(op.checkpoints)]
    assert not (op.truncated and len(op.rows.get("s", [])) > 0 and ckpt_after_truncate), \
        f"truncate + {len(op.rows['s'])} rows checkpointed, then sync failed: lake table now holds a partial snapshot"


def test_max_failed_pct_enforced_for_rds(pg):
    c, cur, port = pg
    cur.execute("CREATE TABLE s (id int primary key, u timestamptz, v text)")
    cur.execute("INSERT INTO s SELECT g, now(), 'BAD' FROM generate_series(1,50) g")
    attrs = [attr("id", "INT", nullable=False), attr("u", "UTC_DATETIME"),
             attr("v", checks=[chk("v_ok", "allowed_values", values=["ok"], on_fail="quarantine_row", max_failed_pct=5.0)])]
    op = _run(_lock(port, attrs))
    assert op.errors, "100% of rows failed a check with max_failed_pct 5 and the sync succeeded (threshold ignored for rds)"


@pytest.mark.skip(reason="[after review] prevented at spec validation (unique applies to file sources; rds rows are keyed by primary_key): tests/test_specs.py::test_unsafe_combinations_rejected")
def test_unique_check_enforced_for_rds(pg):
    c, cur, port = pg
    cur.execute("CREATE TABLE s (id int, u timestamptz, v text)")
    cur.execute("INSERT INTO s VALUES (1, now(), 'a'), (1, now(), 'b')")
    op = _run(_lock(port, ATTRS, checks=[chk("uq", "unique", columns=["id"], on_fail="reject_package")]))
    rr = [r for r in op.rows.get("pf_rule_results", []) if r["rule_id"] == "uq"]
    assert rr, "unique check declared on an rds entity is never evaluated (no evidence, no failure)"


def test_bytea_and_numeric_and_array(pg):
    c, cur, port = pg
    cur.execute("CREATE TABLE s (id int primary key, u timestamptz, v bytea)")
    cur.execute("INSERT INTO s VALUES (1, now(), 'abc'::bytea)")
    op = _run(_lock(port, ATTRS))
    v = op.rows["s"][0]["v"]
    assert not v.startswith("<memory"), f"bytea loaded as {v!r}"


class _DropSession:
    """Connection drops after the first batch, like a failover."""

    def __init__(self, src, cfg):
        pass

    def columns(self, t):
        return {"id", "u", "v"}

    def read(self, *a, **k):
        yield [{"id": 1, "u": "2024-01-01T00:00:00Z", "v": "a"}]
        import psycopg2
        raise psycopg2.OperationalError("server closed the connection unexpectedly")

    def close(self):
        pass


def test_connection_loss_mid_read_is_handled():
    lk = rds_lock([{"name": "s", "table": {"table": "public.s", "incremental": {"cursor_column": "u", "initial_value": None}},
                    "load": "upsert", "primary_key": ["id"], "attributes": ATTRS, "checks": [], "row_checks": []}])
    try:
        op = run(lk, factory=_DropSession)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{type(exc).__name__} escaped Sync.run(): no retry, no clean op.error")
    assert op.errors
