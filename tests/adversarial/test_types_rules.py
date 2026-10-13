"""Type conversion of every Fivetran type, transforms, and every row/attribute check kind."""
from __future__ import annotations

import datetime as dt
import decimal
import time

import pytest

from pf_runtime import cel, rules as R
from pf_runtime.errors import ConfigError, PackageRejected
from pf_runtime.values import TypeConversionError, convert, to_check, to_emit


def C(v, t, **kw):
    return convert(v, {"type": t, **kw})


# ---------------------------------------------------------------- happy paths per type
@pytest.mark.parametrize("v,t,kw,exp", [
    ("abc", "STRING", {}, "abc"),
    (" 42 ", "INT", {}, 42),
    ("-2147483648", "INT", {}, -2147483648),
    ("9223372036854775807", "LONG", {}, 2**63 - 1),
    ("1.5", "DOUBLE", {}, 1.5),
    ("12.345", "DECIMAL", {"precision": 10, "scale": 3}, decimal.Decimal("12.345")),
    ("Yes", "BOOLEAN", {}, True),
    ("0", "BOOLEAN", {}, False),
    ("2024-02-29", "NAIVE_DATE", {}, dt.date(2024, 2, 29)),
    ("01JAN2024", "NAIVE_DATE", {"input_format": "%d%b%Y"}, dt.date(2024, 1, 1)),
    ("2024-01-01T10:00:00", "NAIVE_DATETIME", {}, dt.datetime(2024, 1, 1, 10)),
    ("2024-01-01T10:00:00+02:00", "UTC_DATETIME", {}, dt.datetime(2024, 1, 1, 8, tzinfo=dt.timezone.utc)),
    ('{"a": [1]}', "JSON", {}, {"a": [1]}),
])
def test_convert_ok(v, t, kw, exp):
    assert C(v, t, **kw) == exp


# ---------------------------------------------------------------- must be rejected
@pytest.mark.parametrize("v,t,kw", [
    ("2147483648", "INT", {}),
    ("9223372036854775808", "LONG", {}),
    ("1.5", "INT", {}),
    ("true", "INT", {}),
    ("NaN", "DOUBLE", {}),
    ("1e400", "DOUBLE", {}),
    ("1,5", "DOUBLE", {}),          # decimal comma
    ("1,5", "DECIMAL", {"precision": 5, "scale": 2}),
    ("1000.00", "DECIMAL", {"precision": 5, "scale": 2}),
    ("maybe", "BOOLEAN", {}),
    ("2024-02-30", "NAIVE_DATE", {}),
    ("31/12/2024", "NAIVE_DATE", {}),
    ("{bad", "JSON", {}),
])
def test_convert_rejects(v, t, kw):
    with pytest.raises(TypeConversionError):
        C(v, t, **kw)


# ---------------------------------------------------------------- suspicious acceptance (silent wrong data)
@pytest.mark.parametrize("v,t,kw", [
    ("1_000", "INT", {}),            # Python literal underscores are not a data format
    ("1_0.5", "DOUBLE", {}),
    ("١٢٣", "INT", {}),              # Arabic-Indic digits
    ("１２３", "DOUBLE", {}),         # full-width digits
    ("1e3", "INT", {}),              # scientific notation into an integer column
    ("12.349", "DECIMAL", {"precision": 10, "scale": 2}),  # silently rounded to 12.35
])
def test_convert_should_not_silently_accept(v, t, kw):
    with pytest.raises(TypeConversionError):
        C(v, t, **kw)


def test_decimal_38_digits_supported():
    """DECIMAL(38, 2) is allowed by the spec (precision le=38); a 30-digit value must load."""
    v = "1234567890123456789012345678.12"  # 30 significant digits
    assert C(v, "DECIMAL", precision=38, scale=2) == decimal.Decimal(v)


def test_int_huge_exponent_does_not_hang():
    t0 = time.time()
    with pytest.raises(TypeConversionError):
        C("1e1000000", "INT")
    assert time.time() - t0 < 1.0, f"took {time.time() - t0:.1f}s"


def test_rds_bytes_into_string_not_repr():
    assert C(memoryview(b"abc"), "STRING") in ("abc", "YWJj", "616263"), C(memoryview(b"abc"), "STRING")


def test_rds_bytes_mysql_into_string_not_repr():
    assert C(b"abc", "STRING") in ("abc", "YWJj", "616263"), C(b"abc", "STRING")


def test_date_out_of_range_rejected():
    with pytest.raises(TypeConversionError):
        C("10000-01-01", "NAIVE_DATE")
    assert C("0001-01-01", "NAIVE_DATE") == dt.date(1, 1, 1)


def test_emit_forms():
    assert to_emit(decimal.Decimal("1.10")) == "1.10"
    assert to_emit(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)) == "2024-01-01T00:00:00Z"
    assert to_emit(dt.date(2024, 1, 1)) == "2024-01-01"


# ---------------------------------------------------------------- transforms
def _t(v, ts, cfg=None):
    return R.apply_transforms(v, ts, cfg or {})


def test_pseudonymise_deterministic_and_keyed():
    k = [{"kind": "pseudonymise", "key": {"secret": "s"}}]
    a = _t("SUBJ-1", k, {"s": "key1"})
    assert a == _t("SUBJ-1", k, {"s": "key1"})
    assert a != _t("SUBJ-1", k, {"s": "key2"})
    assert len(a) == 64 and "SUBJ" not in a
    with pytest.raises(ConfigError):
        _t("x", k, {})


def test_map_values_unmapped_modes():
    m = {"kind": "map_values", "mapping": {"M": "Male"}, "unmapped": "keep"}
    assert _t("X", [m]) == "X"
    assert _t("X", [{**m, "unmapped": "null"}]) is None
    with pytest.raises(TypeConversionError):
        _t("X", [{**m, "unmapped": "fail"}])


def test_pad_left_and_null_if_default():
    assert _t("7", [{"kind": "pad_left", "length": 4, "char": "0"}]) == "0007"
    assert _t("12345", [{"kind": "pad_left", "length": 4, "char": "0"}]) == "12345"
    assert _t("NA", [{"kind": "null_if", "values": ["NA"]}, {"kind": "default", "value": "U"}]) == "U"


def _entity(attrs, row_checks=None):
    return {"name": "e", "attributes": attrs, "row_checks": row_checks or []}


def _a(name, t="STRING", **kw):
    return {"name": name, "type": t, "nullable": kw.pop("nullable", True), "transforms": kw.pop("transforms", []),
            "checks": kw.pop("checks", []), **kw}


def test_derive_error_quarantines():
    e = _entity([_a("a", "INT"), _a("b", "INT", transforms=[{"kind": "derive", "expr": "row.a / 0"}])])
    res = R.process_row({"a": "1"}, e, {}, R.Stats())
    assert res.row is None and res.failures[0][0] == "derive__b"


def test_cel_double_vs_int_literal():
    """`row.amount > 0` on a DECIMAL/DOUBLE column: a natural rule. It must not quarantine every row."""
    e = _entity([_a("amount", "DECIMAL", precision=10, scale=2,
                    checks=[{"id": "pos", "kind": "expression", "expr": "row.amount > 0", "on_fail": "quarantine_row", "mode": "enforce"}])])
    res = R.process_row({"amount": "5.00"}, e, {}, R.Stats())
    assert res.row is not None, "5.00 > 0 failed: int literal vs double has no CEL overload, every row quarantined"


# ---------------------------------------------------------------- checks: pass / fail / boundary
@pytest.mark.parametrize("rule,value,ok", [
    ({"kind": "not_null"}, None, False),
    ({"kind": "not_null"}, "", False),
    ({"kind": "not_null"}, 0, True),
    ({"kind": "regex", "pattern": r"\d{4}"}, "1234", True),
    ({"kind": "regex", "pattern": r"\d{4}"}, "12345", False),
    ({"kind": "regex", "pattern": r"\d{4}"}, None, True),
    ({"kind": "allowed_values", "values": ["A", "B"]}, "A", True),
    ({"kind": "allowed_values", "values": ["A", "B"]}, "a", False),
    ({"kind": "range", "min": 1.0, "max": 99.0}, 1, True),
    ({"kind": "range", "min": 1.0, "max": 99.0}, 99, True),
    ({"kind": "range", "min": 1.0, "max": 99.0}, 0, False),
    ({"kind": "range", "min": 1.0, "max": 99.0}, 100, False),
    ({"kind": "range", "min": "2024-01-01", "max": "2024-12-31"}, dt.date(2024, 12, 31), True),
    ({"kind": "range", "min": "2024-01-01", "max": "2024-12-31"}, dt.date(2025, 1, 1), False),
    ({"kind": "length", "min": 2, "max": 3}, "ab", True),
    ({"kind": "length", "min": 2, "max": 3}, "abcd", False),
    ({"kind": "length", "min": 2, "max": 3}, "a", False),
])
def test_check_kinds(rule, value, ok):
    assert R.check_passes({"id": "x", **rule}, value, {"v": value}) is ok


def test_range_datetime_bound_on_same_day():
    """UTC_DATETIME with max '2024-12-31' must accept 2024-12-31T10:00Z? At least it must not accept 2025 values
    while rejecting earlier ones; and a datetime bound written with a space must compare by time, not by character."""
    v = dt.datetime(2024, 6, 1, 12, tzinfo=dt.timezone.utc)
    assert R.check_passes({"id": "x", "kind": "range", "min": "2024-06-01 00:00:00", "max": "2024-06-01 23:59:59"}, v, {}) is True


def test_allowed_values_bool_vs_int():
    assert R.check_passes({"id": "x", "kind": "allowed_values", "values": [1]}, True, {}) is False


@pytest.mark.skip(reason="[after review] numeric bounds on a STRING attribute are rejected at spec validation (test_unsafe_combinations_rejected)")
def test_range_on_numeric_string_attribute():
    """STRING attribute holding '5' with range 1..10 must not compare lexicographically against '10.0'."""
    assert R.check_passes({"id": "x", "kind": "range", "min": 1.0, "max": 10.0}, "5", {}) is True


def test_shadow_counts_but_never_acts():
    e = _entity([_a("v", checks=[{"id": "r", "kind": "regex", "pattern": "A", "on_fail": "reject_package", "mode": "shadow"}])])
    st = R.Stats()
    res = R.process_row({"v": "B"}, e, {}, st)
    assert res.row is not None and st.rules[("r", "e", "v")].outcome == "shadow_failed"


@pytest.mark.parametrize("action,exc", [("reject_package", PackageRejected), ("warn", None), ("quarantine_row", None)])
def test_on_fail_actions(action, exc):
    e = _entity([_a("v", checks=[{"id": "r", "kind": "regex", "pattern": "A", "on_fail": action, "mode": "enforce"}])])
    if exc:
        with pytest.raises(exc):
            R.process_row({"v": "B"}, e, {}, R.Stats())
    else:
        res = R.process_row({"v": "B"}, e, {}, R.Stats())
        assert (res.row is None) is (action == "quarantine_row")


@pytest.mark.parametrize("failed,seen,pct,rejects", [(5, 100, 5.0, False), (6, 100, 5.0, True), (1, 1, 100.0, False), (1, 3, 0.0, True)])
def test_threshold_boundary(failed, seen, pct, rejects):
    e = _entity([_a("v", checks=[{"id": "r", "kind": "regex", "pattern": "A", "on_fail": "quarantine_row", "mode": "enforce", "max_failed_pct": pct}])])
    st = R.Stats()
    for i in range(seen):
        R.process_row({"v": "B" if i < failed else "A"}, e, {}, st)
    if rejects:
        with pytest.raises(PackageRejected):
            R.escalate_thresholds(e, st, seen)
    else:
        R.escalate_thresholds(e, st, seen)
