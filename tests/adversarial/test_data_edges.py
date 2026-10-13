"""Data edge cases through the real engine (CSV parsing + typing + checks)."""
from __future__ import annotations

import pytest

from .helpers import attr, audits, chk, file_entity, file_lock, rows, run, write


def _load(tmp_path, content: bytes | str, entity=None, **pkg):
    write(tmp_path, "d.csv", content)
    return run(file_lock([entity or file_entity()], package=pkg or None), tmp_path)


def test_bom_crlf_quoted_newline_delimiter(tmp_path):
    op = _load(tmp_path, b'\xef\xbb\xbfid,v\r\n1,"a,b"\r\n2,"line1\r\nline2"\r\n')
    assert audits(op)[0][1] == "loaded"
    vs = [r["v"] for r in rows(op)]
    assert vs == ["a,b", "line1\r\nline2"]


def test_empty_file_and_header_only(tmp_path):
    op = _load(tmp_path, b"")
    assert audits(op)[0][1] == "loaded" and rows(op) == []
    (tmp_path / "d.csv").unlink()
    op = _load(tmp_path, "id,v\n")
    assert audits(op)[0][1] == "loaded" and rows(op) == []


def test_duplicate_header_columns_are_rejected(tmp_path):
    """id,v,v: which 'v' is loaded? The file is ambiguous and must not load silently."""
    op = _load(tmp_path, "id,v,v\n1,first,second\n")
    assert audits(op)[0][1] == "rejected", f"loaded silently: {rows(op)}"


def test_header_case_and_whitespace(tmp_path):
    op = _load(tmp_path, " id , v \n1,x\n")
    assert audits(op)[0][1] == "loaded"


def test_wrong_encoding_rejected(tmp_path):
    op = _load(tmp_path, "id,v\n1,caf\xe9\n".encode("latin-1"))
    assert audits(op)[0][1] == "rejected"


def test_utf8_spelled_utf_8_underscore_bom(tmp_path):
    """'utf_8' is a valid Python codec name; the BOM must still be tolerated."""
    e = file_entity(fmt={"encoding": "utf_8"})
    op = _load(tmp_path, b"\xef\xbb\xbfid,v\n1,x\n", entity=e)
    assert audits(op)[0][1] == "loaded", audits(op)


def test_single_column_empty_value_row_is_not_dropped(tmp_path):
    """Single-column file: an empty (null) value line is a row, not a blank line."""
    e = file_entity(attrs=[attr("v")])
    op = _load(tmp_path, "v\na\n\nb\n", entity=e)
    # 3 records were sent (a, <null>, b); read count must include the null row (or quarantine it), never drop it
    ent = op.rows["pf_entity_audit"][0]
    assert ent["rows_read"] == 3, f"rows_read={ent['rows_read']} emitted={[r['v'] for r in rows(op)]}"


def test_very_wide_and_long_values(tmp_path):
    big = "x" * 5_000_000
    header = ",".join(["id"] + [f"c{i}" for i in range(500)])
    e = file_entity(attrs=[attr("id")] + [attr(f"c_{i}", source=f"c{i}") for i in range(500)])
    op = _load(tmp_path, header + "\n" + ",".join(["1", big] + ["y"] * 499) + "\n", entity=e)
    assert audits(op)[0][1] == "loaded" and len(rows(op)[0]["c_0"]) == 5_000_000


def test_unicode_and_leading_zeros(tmp_path):
    e = file_entity(attrs=[attr("id"), attr("v"), attr("n", "INT")])
    op = _load(tmp_path, "id,v,n\n007,Zoë 東京 🧪,007\n", entity=e)
    r = rows(op)[0]
    assert r["id"] == "007" and r["v"] == "Zoë 東京 🧪" and r["n"] == 7


def test_ragged_rows_quarantined(tmp_path):
    op = _load(tmp_path, "id,v\n1,a\n2\n3,c,extra\n")
    assert [r["id"] for r in rows(op)] == ["1"]
    assert len(op.rows["pf_quarantine"]) == 2


def test_unterminated_quote_rejects_package(tmp_path):
    op = _load(tmp_path, 'id,v\n1,"abc\n')
    assert audits(op)[0][1] in ("rejected",)


def test_null_tokens_and_whitespace(tmp_path):
    e = file_entity(attrs=[attr("id"), attr("v", "INT")], fmt={"null_tokens": ["", "NA", "NULL"]})
    op = _load(tmp_path, "id,v\n1,NA\n2,NULL\n3, \n4,\n", entity=e)
    got = {r["id"]: r["v"] for r in rows(op)}
    assert got == {"1": None, "2": None, "4": None}  # " " is not a null token -> INT type error -> quarantine
    assert len(op.rows["pf_quarantine"]) == 1


def test_jsonl_unknown_keys_detected(tmp_path):
    """JSON members bypass the header check: a renamed key silently loads nulls."""
    e = file_entity(member=r".*\.jsonl", fmt={"type": "jsonl"}, attrs=[attr("id"), attr("v")])
    write(tmp_path, "d.jsonl", '{"id": "1", "value_renamed": "x"}\n')
    op = run(file_lock([e], package={"match": r".*\.jsonl"}), tmp_path)
    st = audits(op)[0][1]
    assert st == "rejected", f"status={st} rows={rows(op)}"


def test_json_object_into_string_column_is_json_not_python_repr(tmp_path):
    e = file_entity(member=r".*\.jsonl", fmt={"type": "jsonl"}, attrs=[attr("id"), attr("v")])
    write(tmp_path, "d.jsonl", '{"id": "1", "v": {"a": true}}\n')
    op = run(file_lock([e], package={"match": r".*\.jsonl"}), tmp_path)
    v = rows(op)[0]["v"] if rows(op) else None
    assert v in (None, '{"a": true}', '{"a":true}'), f"STRING got Python repr {v!r}"
