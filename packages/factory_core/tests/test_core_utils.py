from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from factory_core.envelope import Issue, Result
from factory_core.pathmatch import PatternError, match, may_overlap, parse_path_pattern
from factory_core.templates import TemplateError, parse_template, render
from factory_core.uri import parse_destination_uri
from hypothesis import given
from hypothesis import strategies as st

from factory_core import canonical, cel, errors, units, yamlio

# ---------------------------------------------------------------- errors / envelope


def test_exit_codes_per_class() -> None:
    assert errors.exit_code_for(None) == 0
    assert errors.exit_code_for(errors.TransientError("x", "m")) == 75
    assert errors.exit_code_for(errors.DataError("x", "m")) == 65
    assert errors.exit_code_for(errors.ConfigError("x", "m")) == 78
    assert errors.exit_code_for(errors.SecurityError("x", "m")) == 77
    assert errors.exit_code_for(RuntimeError("boom")) == 70
    assert {errors.ErrorClass.TRANSIENT} == errors.RETRYABLE


def test_envelope_shape() -> None:
    r = Result(command="validate", ok=False, issues=[Issue(code="required", msg="m", hint="h")])
    d = r.model_dump(mode="json")
    assert d["api_version"] == "factory/v1"
    assert r.errors[0].hint == "h"


# ---------------------------------------------------------------- canonical


def test_canonical_rejects_non_json_types() -> None:
    for bad in (1.5, Decimal("1"), dt.date(2024, 1, 1), {1: "a"}, object()):
        with pytest.raises(canonical.CanonicalError):
            canonical.canonical_bytes({"x": bad})


@given(st.dictionaries(st.text(min_size=1, max_size=5), st.integers() | st.text(max_size=5), max_size=8))
def test_canonical_independent_of_key_order(d: dict[str, object]) -> None:
    reordered = dict(reversed(list(d.items())))
    assert canonical.canonical_bytes(d) == canonical.canonical_bytes(reordered)


def test_doc_sha_depends_on_kind_and_header() -> None:
    body = {"a": 1}
    assert canonical.doc_sha256("x", body) != canonical.doc_sha256("y", body)
    assert canonical.doc_sha256("x", body, env="dev") != canonical.doc_sha256("x", body, env="prod")
    assert canonical.pretty_json({"b": 1, "a": 2}).endswith("\n")


# ---------------------------------------------------------------- units


@pytest.mark.parametrize(
    ("text", "value"),
    [("10GB", 10**10), ("10000MB", 10**10), ("512MiB", 512 * 2**20), ("1.5GB", 1_500_000_000), ("0B", 0)],
)
def test_parse_bytes(text: str, value: int) -> None:
    assert units.parse_bytes(text) == value


@pytest.mark.parametrize("bad", ["10gb", "10", "1.5B", "ten GB", "10 GBs", ""])
def test_parse_bytes_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        units.parse_bytes(bad)


def test_durations_and_retention() -> None:
    assert units.parse_duration("1h30m") == 5400
    assert units.parse_duration("15m") == units.parse_duration("900s")
    assert units.format_duration(5400) == "1h30m"
    assert units.parse_retention_days("10y") == 3650
    for bad in ("", "1x", "m", "90"):
        with pytest.raises(ValueError):
            units.parse_duration(bad)


# ---------------------------------------------------------------- yamlio


def test_yaml_round_trip_keeps_comments_and_order() -> None:
    text = "# head\nb: 1  # keep me\na:\n  - x\n  - y\nlong: " + " ".join(["w"] * 100) + "\n"
    assert yamlio.dump_text(yamlio.load_text(text)) == text


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("a: &x 1\nb: *x\n", "yaml_alias"),
        ("a: !!python/object:os.system 1\n", "yaml_tag"),
        ("a: !custom 1\n", "yaml_tag"),
        ("a: 1\na: 2\n", "yaml_duplicate_key"),
        ("a: [1\n", "yaml_syntax"),
    ],
)
def test_yaml_rejections(text: str, code: str) -> None:
    with pytest.raises(yamlio.YamlError) as ei:
        yamlio.load_text(text)
    assert ei.value.code == code


def test_yaml_to_plain_dates_and_keys() -> None:
    with pytest.raises(yamlio.YamlError) as ei:
        yamlio.to_plain(yamlio.load_text("d: 2024-03-01\n"))
    assert ei.value.code == "yaml_date"
    with pytest.raises(yamlio.YamlError) as ei:
        yamlio.to_plain(yamlio.load_text("1: x\n"))
    assert ei.value.code == "yaml_key_type"
    assert yamlio.to_plain(yamlio.load_text("a: '2024-03-01'\nb: [1, true, null]\n")) == {
        "a": "2024-03-01",
        "b": [1, True, None],
    }


def test_yaml_size_cap() -> None:
    with pytest.raises(yamlio.YamlError) as ei:
        yamlio.load_text("a: x\n", max_bytes=3)
    assert ei.value.code == "yaml_too_large"


# ---------------------------------------------------------------- CEL


def test_cel_null_aware_and_fail_closed() -> None:
    assert cel.evaluate("row.n != null && row.n > 1", {"row": {"n": 2}}) == (True, True)
    assert cel.evaluate("row.n != null && row.n > 1", {"row": {"n": None}})[0] is False
    assert cel.evaluate("1 + 1", {})[0] is False  # non-boolean fails
    with pytest.raises(cel.CelSyntaxError):
        cel.parse("a ==")


def test_cel_referenced_paths() -> None:
    expr = "artifacts.listing.count == vars.expected_files && size(run.x) > 0 || vars.a.b[0] == 'x'"
    assert cel.referenced_paths(expr) == {
        ("artifacts", "listing", "count"),
        ("vars", "expected_files"),
        ("run", "x"),
        ("vars", "a", "b"),
    }


# ---------------------------------------------------------------- templates


def test_template_parse_and_render() -> None:
    t = parse_template("{study_id}_{package_id}_{seq:04d}.zip")
    assert t.names == {"study_id", "package_id", "seq"}
    assert render(t, {"study_id": "ONC101", "package_id": "PKG1", "seq": 7}) == "ONC101_PKG1_0007.zip"
    assert render(parse_template("a{{b}}c"), {}) == "a{b}c"


@pytest.mark.parametrize(
    "bad",
    ["{x.__class__}", "{x[0]}", "{Study}", "{study", "study}", "{study_id:>10}", "{name:04d}", "{}", "{0}"],
)
def test_template_rejects(bad: str) -> None:
    with pytest.raises(TemplateError):
        parse_template(bad)


def test_template_path_safe_values() -> None:
    t = parse_template("studies/{study_id}")
    with pytest.raises(TemplateError) as ei:
        render(t, {"study_id": "../etc"}, path_safe=True)
    assert ei.value.code == "template_unsafe_value"


# ---------------------------------------------------------------- destination URIs (security-critical)

GOOD = [
    ("s3://acme-delivery-test/studies/ONC101/in/", ("s3", "acme-delivery-test", None, ("studies", "ONC101", "in"))),
    (
        "sftp://sftp.partner-cro.example.com/inbound/acme/ONC101/",
        ("sftp", "sftp.partner-cro.example.com", 22, ("inbound", "acme", "ONC101")),
    ),
    (
        "https://Upload.Partner-CRO.example.com:8443/api/v2/x",
        ("https", "upload.partner-cro.example.com", 8443, ("api", "v2", "x")),
    ),
]

BAD = [
    "",
    "ftp://host.example.com/x",
    "file:///etc/passwd",
    "http://host.example.com/x",
    "s3:/bucket/x",
    "s3://Bucket/x",
    "s3://bu/x",
    "s3://bucket:443/x",
    "s3://bucket..name/x",
    "sftp://host.example.com/../etc",
    "sftp://host.example.com/a/./b",
    "sftp://host.example.com/a//b",
    "sftp://host.example.com/%2e%2e/x",
    "sftp://host.example.com/a%2Fb",
    "sftp://user@host.example.com/x",
    "https://good.example.com@evil.example.com/x",
    "https://host.example.com/x?y=1",
    "https://host.example.com/x#frag",
    "https://host.example.com/x;y",
    "https://host.example.com/a\\b",
    "https://host.example.com/a b",
    "https://host.example.com/a\tb",
    "https://host.example.com./x",
    "https://127.0.0.1/x",
    "https://[::1]/x",
    "https://10.0.0.1:443/x",
    "https://localhost/x",
    "https://host.example.com:0/x",
    "https://host.example.com:65536/x",
    "https://host.example.com:080/x",
    "https://host.example.com:abc/x",
    "https://gооgle.example.com/x",  # Cyrillic о
    "https://xn--host.example.com/x?",
    "https://-bad.example.com/x",
    "https://bad-.example.com/x",
    "https://host.example.com/x$y",
    "https://host.example.com/x*",
    "https://host.example.com/x\x00",
    "sftp://host.example.com:22:23/x",
    "https:/host.example.com/x",
    "https://",
    "s3://",
]


@pytest.mark.parametrize(("text", "expected"), GOOD)
def test_uri_accepts(text: str, expected: tuple[object, ...]) -> None:
    u = parse_destination_uri(text)
    assert (u.type, u.host, u.port, u.segments) == expected


@pytest.mark.parametrize("text", BAD)
def test_uri_rejects(text: str) -> None:
    with pytest.raises(errors.SecurityError) as ei:
        parse_destination_uri(text)
    assert ei.value.code == "destination_uri_invalid"


# ---------------------------------------------------------------- path patterns


def test_pattern_match_binds_variables() -> None:
    p = parse_path_pattern("studies/{study_id}/**", absolute=False)
    assert p.literal_root == "studies"
    assert match(p, ("studies", "ONC101", "in"), {"study_id": "ONC101"})
    assert match(p, ("studies", "ONC101"), {"study_id": "ONC101"})
    assert not match(p, ("studies", "ONC999", "in"), {"study_id": "ONC101"})
    assert not match(p, ("other", "ONC101"), {"study_id": "ONC101"})
    star = parse_path_pattern("/api/v2/studies/{study_id}/packages/*", absolute=True)
    assert match(star, ("api", "v2", "studies", "S1", "packages", "P1"), {"study_id": "S1"})
    assert not match(star, ("api", "v2", "studies", "S1", "packages"), {"study_id": "S1"})
    assert not match(star, ("api", "v2", "studies", "S1", "packages", "P1", "x"), {"study_id": "S1"})


@pytest.mark.parametrize(
    ("src", "absolute", "code"),
    [
        ("**", False, "destination_pattern_too_broad"),
        ("{study_id}/x", False, "destination_pattern_too_broad"),
        ("/**", True, "destination_pattern_too_broad"),
        ("studies/**/x", False, "destination_pattern_invalid"),
        ("/inbound", False, "destination_pattern_invalid"),
        ("inbound", True, "destination_pattern_invalid"),
        ("a/../b", False, "destination_pattern_invalid"),
        ("a/{Bad}", False, "destination_pattern_invalid"),
    ],
)
def test_pattern_rejects(src: str, absolute: bool, code: str) -> None:
    with pytest.raises(PatternError) as ei:
        parse_path_pattern(src, absolute=absolute)
    assert ei.value.code == code


def test_pattern_overlap() -> None:
    a = parse_path_pattern("studies/{study_id}/**", absolute=False)
    b = parse_path_pattern("studies/fixed/x", absolute=False)
    c = parse_path_pattern("archive/{study_id}/**", absolute=False)
    assert may_overlap(a, b)
    assert not may_overlap(a, c)


@given(st.lists(st.sampled_from(["studies", "ONC101", "..", ".", "x", "in"]), max_size=6))
def test_match_never_accepts_traversal(segments: list[str]) -> None:
    p = parse_path_pattern("studies/{study_id}/**", absolute=False)
    if match(p, tuple(segments), {"study_id": "ONC101"}):
        assert segments[:2] == ["studies", "ONC101"]
