"""Runtime: CEL semantics, archive safety, break-and-verify on every shipped example."""
from __future__ import annotations

import io
import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

from pf_runtime import cel

FRAMEWORK = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("expr,row,ok", [
    ("row.n != null", {"n": 0}, True),
    ("row.n != null", {"n": None}, False),
    ("row.n == null", {"n": 5}, False),
    ("null == row.n", {"n": 5}, False),
    ("row.d != null && row.d >= 0.0", {"d": 1.5}, True),
    ("row.s != null && row.s.matches('^[A-Z]{2}$')", {"s": "GB"}, True),
    ("row.n < 3", {"n": None}, False),          # ordering against null errors -> fails closed
    ("row.n == 1.0", {"n": 1}, False),          # int vs double is not an overload -> fails closed
    ("row.missing == 1", {}, False),            # unknown member -> fails closed
    ("timestamp(row.r + 'T00:00:00Z') - timestamp(row.v + 'T00:00:00Z') <= duration('240h')",
     {"v": "2024-01-01", "r": "2024-01-11"}, True),
    ("1", {}, False),                           # non-boolean result fails
])
def test_cel_semantics(expr, row, ok):
    assert cel.evaluate(expr, {"row": row})[0] is ok


def _zip(members: dict[str, bytes], symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in members.items():
            z.writestr(n, d)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            z.writestr(info, "/etc/passwd")
    return buf.getvalue()


@pytest.mark.parametrize("members,symlink,params", [
    ({"../../evil.txt": b"x"}, None, {}),
    ({"/abs.txt": b"x"}, None, {}),
    ({"a.csv": b"x"}, "link.csv", {}),
    ({"bomb.csv": b"0" * 2_000_000}, None, {"max_ratio": 10}),
    ({f"f{i}.csv": b"x" for i in range(5)}, None, {"max_members": 3}),
    ({"inner.zip": _zip({"a": b"b"})}, None, {}),
])
def test_unzip_refuses_unsafe_archives(tmp_path, members, symlink, params):
    from pf_runtime import package as P
    from pf_runtime.errors import PackageRejected
    from pf_runtime.sources import RemoteFile

    data = _zip(members, symlink)
    local = tmp_path / "package.bin"
    local.write_bytes(data)
    lp = P.LocalPackage(P.Package(RemoteFile("d/x.zip", len(data), 0.0), {}, ()), str(tmp_path), str(local), "sha", "md5")
    with pytest.raises(PackageRejected):
        P.unpack(lp, [{"kind": "unzip", "max_members": 50, "max_total_bytes": 10**9, "max_ratio": 200, **params}])


@pytest.mark.parametrize("name", ["oncology_labs", "site_monitoring", "trial_sites"])
def test_examples_break_and_verify(tmp_path, name, monkeypatch):
    from pf.bundle import load_bundle
    from pf.generate import render_all, write
    from pf.testing import run_tests

    (tmp_path / "pf.toml").write_text("")
    shutil.copytree(FRAMEWORK / "examples" / "pipelines" / name, tmp_path / "pipelines" / name, ignore=shutil.ignore_patterns("build"))
    monkeypatch.chdir(tmp_path)
    b = load_bundle(tmp_path / "pipelines" / name)
    write(tmp_path, render_all(b, tmp_path), name)
    out = run_tests(tmp_path, b)
    assert out["report"]["ok"], [i.msg for i in out["issues"]]
    assert out["report"]["uncovered"] == []


def test_lying_zip_header_is_caught(tmp_path):
    """A member whose central-directory sizes understate its real size is rejected, not trusted."""
    from pf_runtime import package as P
    from pf_runtime.errors import PackageRejected
    from pf_runtime.sources import RemoteFile

    data = bytearray(_zip({"a.csv": b"0" * 3_000_000}))
    i = data.rfind(b"PK\x01\x02")  # central directory header: uncompressed size at offset 24
    data[i + 24:i + 28] = (100).to_bytes(4, "little")
    j = data.find(b"PK\x03\x04")  # local header: same field at offset 22
    data[j + 22:j + 26] = (100).to_bytes(4, "little")
    local = tmp_path / "package.bin"
    local.write_bytes(bytes(data))
    lp = P.LocalPackage(P.Package(RemoteFile("x.zip", len(data), 0.0), {}, ()), str(tmp_path), str(local), "s", "m")
    with pytest.raises(PackageRejected):
        P.unpack(lp, [{"kind": "unzip", "max_members": 5, "max_total_bytes": 10**9, "max_ratio": 200}])


def test_recorder_enforces_fivetran_contract():
    from pf.testing import RecorderOp

    op = RecorderOp([{"table": "t", "primary_key": ["id"], "columns": {}}])
    op.upsert("t", {"id": "1"})
    for bad in ({"id": None}, {"id": ""}):
        with pytest.raises(AssertionError):
            op.upsert("t", bad)
    with pytest.raises(AssertionError):
        op.upsert("undeclared", {"id": "1"})


def test_evidence_never_has_empty_keys():
    from pf_runtime.engine import EVIDENCE_TABLES, LINEAGE

    names = [c for t in EVIDENCE_TABLES for c in t["columns"]] + list(LINEAGE)
    import re

    assert not [n for n in names if re.search(r"[a-z][0-9]|[0-9][a-z]", n)]  # Fivetran would rename them
