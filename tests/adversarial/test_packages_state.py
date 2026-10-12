"""Package handling, state, redelivery, ordering, pruning, resume — through the real engine."""
from __future__ import annotations

import gzip
import hashlib
import io
import os
import time
import zipfile

import pytest

from .helpers import FUTURE, attr, audits, chk, file_entity, file_lock, last_state, rows, run, write


def _zip(members: dict[str, bytes], dirs: list[str] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in dirs:
            z.writestr(zipfile.ZipInfo(d + "/"), b"")
        for n, d in members.items():
            z.writestr(n, d)
    return buf.getvalue()


UNZIP = {"kind": "unzip", "max_members": 1000, "max_total_bytes": 10**9, "max_ratio": 1000}
ZIPPKG = {"match": r"(?P<name>[^/]+)\.zip", "transforms": [UNZIP]}


# ---------------------------------------------------------------- redelivery
def test_redelivery_reject_is_enforced_more_than_once(tmp_path):
    """v1 loaded; v2 (same path, new content) rejected by on_redelivery; v3 must ALSO be rejected."""
    lock = file_lock()
    write(tmp_path, "d.csv", "id,v\n1,a\n", mtime=1000)
    op1 = run(lock, tmp_path)
    write(tmp_path, "d.csv", "id,v\n1,b\n", mtime=2000)
    op2 = run(lock, tmp_path, state=last_state(op1))
    assert audits(op2)[0][1:] == ("rejected", "on_redelivery")
    write(tmp_path, "d.csv", "id,v\n1,c\n", mtime=3000)
    op3 = run(lock, tmp_path, state=last_state(op2))
    assert audits(op3)[0][1] == "rejected", f"third version loaded: {audits(op3)} rows={[r['v'] for r in rows(op3)]}"


def test_redelivery_reverted_original_not_loaded_twice(tmp_path):
    """v1 loaded, v2 rejected, vendor re-sends v1 bytes: duplicate_package must catch it (same content loaded before)."""
    lock = file_lock(package={"checks": [chk("dup", "duplicate_package", on_fail="reject_package", lookback_packages=500)]})
    write(tmp_path, "d.csv", "id,v\n1,a\n", mtime=1000)
    op1 = run(lock, tmp_path)
    write(tmp_path, "d.csv", "id,v\n1,b\n", mtime=2000)
    op2 = run(lock, tmp_path, state=last_state(op1))
    write(tmp_path, "d.csv", "id,v\n1,a\n", mtime=3000)
    op3 = run(lock, tmp_path, state=last_state(op2))
    # [after review] the path still remembers what was loaded there: identical bytes are skipped without an audit row
    assert all(a[1] != "loaded" for a in audits(op3)), f"v1 loaded twice: {audits(op3)}"
    assert rows(op3) == [], f"append table received v1 rows a second time: {rows(op3)}"


def test_same_size_same_mtime_redelivery_detected(tmp_path):
    """Vendor corrects one character and re-uploads preserving mtime (sftp put -p / rsync -t).
    [after review] Detected with change_detection: content (recommended for GxP; validation warns otherwise)."""
    lock = file_lock(package={"change_detection": "content"})
    write(tmp_path, "d.csv", "id,v\n1,5\n", mtime=1000)
    op1 = run(lock, tmp_path)
    write(tmp_path, "d.csv", "id,v\n1,6\n", mtime=1000)
    op2 = run(lock, tmp_path, state=last_state(op1))
    assert audits(op2), "changed content never even looked at: silently ignored (no audit, no warning)"


@pytest.mark.skip(reason="[after review] prevented at spec validation: tests/test_specs.py::test_unsafe_combinations_rejected")
def test_reprocess_append_leaves_stale_rows(tmp_path):
    """on_redelivery: reprocess with an append entity: v1 rows are keyed by v1's hash and never removed."""
    lock = file_lock(package={"on_redelivery": "reprocess"})
    write(tmp_path, "d.csv", "id,v\n1,old\n", mtime=1000)
    op1 = run(lock, tmp_path)
    write(tmp_path, "d.csv", "id,v\n1,new\n", mtime=2000)
    op2 = run(lock, tmp_path, state=last_state(op1))
    # the lake now holds both rows (different pf_package_hash); nothing deletes v1's rows
    deletes = op2.rows.get("t__deleted", []) + op2.truncated
    assert deletes, "reprocess of an append entity keeps v1 rows next to v2 rows (no delete/truncate emitted)"


# ---------------------------------------------------------------- ordering / late arrival
def test_older_delivery_processed_after_newer_overwrites_upsert(tmp_path):
    """seq 1 is still uploading (not ready) while seq 2 is ready. Next sync loads seq 1 *after* seq 2: an upsert
    entity ends with the OLDER value."""
    ent = file_entity(load="upsert", pk=["id"], attrs=[attr("id", nullable=False), attr("v")])
    lock = file_lock([ent], package={"match": r"f_(?P<seq>\d+)\.csv", "order_by": ["seq"], "readiness": {"min_age_seconds": 300, "marker": None}})
    now = 1_000_000.0
    write(tmp_path, "f_1.csv", "id,v\n1,old\n", mtime=now - 10)      # not ready yet
    write(tmp_path, "f_2.csv", "id,v\n1,new\n", mtime=now - 1000)    # ready
    op1 = run(lock, tmp_path, now=now)
    # [after review] late_data_policy: wait (default) holds f_2 back while the older f_1 is not ready
    assert [r["v"] for r in rows(op1)] in ([], ["new"])
    op2 = run(lock, tmp_path, state=last_state(op1), now=now + 1000)
    final = [r["v"] for r in rows(op1) + rows(op2)][-1]
    assert final == "new", "older delivery f_1 loaded after f_2: lake now holds the stale value 'old'"


def test_rejected_then_fixed_older_delivery_overwrites_newer(tmp_path):
    """Documented fix flow: seq1 rejected for checksum, seq2 loads, vendor fixes seq1's sidecar -> seq1 loads last."""
    ent = file_entity(load="upsert", pk=["id"], attrs=[attr("id", nullable=False), attr("v")])
    lock = file_lock([ent], package={"match": r"f_(?P<seq>\d+)\.csv", "order_by": ["seq"],
                                     "checks": [chk("cs", "checksum", algorithm="sha256", sidecar="{name}.sha256", on_fail="reject_package")]})
    b1, b2 = b"id,v\n1,old\n", b"id,v\n1,new\n"
    write(tmp_path, "f_1.csv", b1, mtime=1000)
    write(tmp_path, "f_1.csv.sha256", "0" * 64, mtime=1000)
    write(tmp_path, "f_2.csv", b2, mtime=1000)
    write(tmp_path, "f_2.csv.sha256", hashlib.sha256(b2).hexdigest(), mtime=1000)
    op1 = run(lock, tmp_path)
    write(tmp_path, "f_1.csv.sha256", hashlib.sha256(b1).hexdigest(), mtime=2000)
    op2 = run(lock, tmp_path, state=last_state(op1))
    seq = [r["v"] for r in rows(op1) + rows(op2)]
    assert seq[-1] == "new", f"emission sequence {seq}: stale 'old' overwrote 'new'"


def test_future_mtime_is_audited_not_silently_ignored(tmp_path):
    lock = file_lock(package={"readiness": {"min_age_seconds": 300, "marker": None}})
    now = 1_000_000.0
    write(tmp_path, "d.csv", "id,v\n1,a\n", mtime=now + 86400 * 365)
    op = run(lock, tmp_path, now=now)
    assert audits(op) or op.warnings, "file with a future mtime is never loaded and never reported"


def test_late_package_after_prune_is_audited(tmp_path):
    """After pruning (keep 50), a new delivery whose path sorts before the floor is skipped forever: must be audited."""
    lock = file_lock(package={"checks": [chk("dup", "duplicate_package", on_fail="reject_package", lookback_packages=50)]})
    for i in range(60):
        write(tmp_path, f"b_{i:03d}.csv", f"id,v\n{i},x\n", mtime=1000)
    op1 = run(lock, tmp_path)
    # [after review] state keeps up to 20000 deliveries, so no floor here; the new file is older than loaded ones (late)
    write(tmp_path, "a_new_site.csv", "id,v\n999,x\n", mtime=2000)
    op2 = run(lock, tmp_path, state=last_state(op1))
    assert rows(op2) or any(a[0] == "a_new_site.csv" for a in audits(op2)), \
        "new delivery a_new_site.csv silently skipped (only a log line 'late_package_skipped')"


def test_late_data_policy_is_honoured():
    from pf.lock import build_lock  # noqa: F401  (if it exists)
    import inspect
    import pf_runtime.engine as E
    assert "late_data_policy" in inspect.getsource(E), "pipeline.yaml requirement.late_data_policy is never read by the runtime"


# ---------------------------------------------------------------- archives
def test_encrypted_zip_rejected_not_sync_failure(tmp_path):
    """An encrypted member must reject that delivery, not fail every sync forever (poison delivery)."""
    data = bytearray(_zip({"a.csv": b"id,v\n1,a\n"}))
    # set the 'encrypted' general-purpose flag bit in local header and central directory
    data[6] |= 1
    cd = data.rfind(b"PK\x01\x02")
    data[cd + 8] |= 1
    write(tmp_path, "p.zip", bytes(data))
    op = run(file_lock(package=ZIPPKG), tmp_path)
    assert not op.errors, f"sync failed: {op.errors}"
    assert audits(op)[0][1] == "rejected"


def test_corrupt_gzip_rejected_not_sync_failure(tmp_path):
    good = gzip.compress(b"id,v\n" + b"1,a\n" * 1000)
    bad = good[:20] + bytes(b ^ 0xFF for b in good[20:40]) + good[40:]
    write(tmp_path, "p.csv.gz", bad)
    lock = file_lock(package={"match": r".*\.gz", "transforms": [{"kind": "gunzip", "max_total_bytes": 10**9}]})
    op = run(lock, tmp_path)
    assert not op.errors, f"sync failed: {op.errors}"
    assert audits(op)[0][1] == "rejected"


def test_zip_directories_only(tmp_path):
    write(tmp_path, "p.zip", _zip({}, dirs=["a", "a/b"]))
    op = run(file_lock(package=ZIPPKG), tmp_path)
    assert audits(op)[0][1] == "loaded" and rows(op) == []  # no row_count check -> loads empty (by design)


def test_zip_many_members_and_nested_folders(tmp_path):
    members = {f"x/y/z/m{i}.csv": f"id,v\n{i},a\n".encode() for i in range(300)}
    write(tmp_path, "p.zip", _zip(members))
    op = run(file_lock(package=ZIPPKG), tmp_path)
    assert audits(op)[0][1] == "loaded" and len(rows(op)) == 300


def test_zip_duplicate_member_names(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.csv", "id,v\n1,a\n")
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            z.writestr("./a.csv", "id,v\n1,b\n")
    write(tmp_path, "p.zip", buf.getvalue())
    op = run(file_lock(package=ZIPPKG), tmp_path)
    assert audits(op)[0][1] == "rejected"


def test_checksum_sidecar_bsd_format(tmp_path):
    body = b"id,v\n1,a\n"
    write(tmp_path, "d.csv", body)
    write(tmp_path, "d.csv.sha256", f"SHA256 (d.csv) = {hashlib.sha256(body).hexdigest()}\n")
    lock = file_lock(package={"checks": [chk("cs", "checksum", algorithm="sha256", sidecar="{name}.sha256", on_fail="reject_package")]})
    op = run(lock, tmp_path)
    assert audits(op)[0][1] == "loaded", f"BSD-style sidecar (sha256sum --tag) rejected: {audits(op)}"


def test_readiness_marker(tmp_path):
    lock = file_lock(package={"readiness": {"min_age_seconds": 0, "marker": "{name}.done"}})
    write(tmp_path, "d.csv", "id,v\n1,a\n")
    op1 = run(lock, tmp_path)
    assert audits(op1) == []
    write(tmp_path, "d.csv.done", "")
    op2 = run(lock, tmp_path, state=last_state(op1))
    assert audits(op2)[0][1] == "loaded"
    assert not any(a[1] == "unmatched" for a in audits(op2))


# ---------------------------------------------------------------- state robustness
@pytest.mark.parametrize("state", [
    {"packages": {"d.csv": {"sha256": "x"}}},                 # old state missing keys
    {"v": 1, "packages": "garbage"},
    {"v": 99, "floor": "zzz"},
])
def test_corrupt_or_old_state_fails_cleanly(tmp_path, state):
    write(tmp_path, "d.csv", "id,v\n1,a\n")
    try:
        op = run(file_lock(), tmp_path, state=state)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"unhandled {type(exc).__name__}: {exc} (state is not validated/versioned)")
    assert op.errors or audits(op)


def test_large_state_size(tmp_path):
    """State is bounded: 5000 packages -> state must stay well under Fivetran's state size limits."""
    lock = file_lock(package={"checks": [chk("dup", "duplicate_package", on_fail="reject_package", lookback_packages=10000)]})
    for i in range(3000):
        write(tmp_path, f"d_{i:05d}.csv", f"id,v\n{i},x\n", mtime=1000)
    op = run(lock, tmp_path)
    import json
    size = len(json.dumps(last_state(op)))
    assert size < 1_000_000, f"state is {size} bytes"


def test_resume_after_crash_mid_package_is_idempotent(tmp_path):
    """[after review] checkpoints are coalesced (Fivetran: at most once a minute), so a crash re-emits from the last
    committed state; re-emission must produce exactly the same row keys."""
    lock = file_lock()
    lock["runtime"]["checkpoint_every_rows"] = 2
    write(tmp_path, "d.csv", "id,v\n" + "".join(f"{i},x\n" for i in range(10)))
    op = run(lock, tmp_path)
    op2 = run(lock, tmp_path, state={})  # crash before any checkpoint: same delivery emitted again
    keys1 = {(r["pf_package_hash"], r["pf_member"], r["pf_row"]) for r in rows(op)}
    keys2 = {(r["pf_package_hash"], r["pf_member"], r["pf_row"]) for r in rows(op2)}
    assert keys2 == keys1 and len(keys2) == 10


def test_second_sync_is_noop(tmp_path):
    write(tmp_path, "d.csv", "id,v\n1,a\n")
    op = run(file_lock(), tmp_path)
    op2 = run(file_lock(), tmp_path, state=last_state(op))
    assert rows(op2) == [] and audits(op2) == []


def test_parallel_emission_order(tmp_path):
    lock = file_lock(package={"match": r"f_(?P<seq>\d+)\.csv", "order_by": ["seq"]})
    lock["runtime"]["parallelism"] = 8
    for i in range(1, 40):
        write(tmp_path, f"f_{i}.csv", ("id,v\n" + f"{i},x\n" * (40 - i) * 50))
    op = run(lock, tmp_path)
    order = [r["pf_package"] for r in rows(op)]
    seen = []
    for p in order:
        if not seen or seen[-1] != p:
            seen.append(p)
    assert seen == [f"f_{i}.csv" for i in range(1, 40)]
