"""retry, jsonlog, runkey, globmatch and cel.evaluate_value (Phase 3 additions to factory_core)."""

from __future__ import annotations

import io
import json
import logging
import random
import re
import ssl
import threading
from datetime import UTC, datetime
from typing import Any

import pytest
from factory_core.errors import ConfigError, DataError, ErrorClass, SecurityError, TransientError
from factory_core.globmatch import GlobError, glob_match
from factory_core.jsonlog import MASK, JsonLogger, Redactor, bind, configure, get_logger, register_secret
from factory_core.retry import (
    Backoff,
    call_with_retry,
    classify,
    classify_http_status,
    full_jitter,
    parse_retry_after,
)
from factory_core.runkey import run_dir, run_key_hash
from hypothesis import given
from hypothesis import strategies as st

from factory_core import cel, jsonlog

# ---------------------------------------------------------------- retry: classification


class SSHException(Exception):  # stand-ins named like the client library classes
    pass


class BadHostKeyException(SSHException):
    pass


class AuthenticationException(SSHException):
    pass


class ConnectError(Exception):  # httpx.ConnectError
    pass


class NetworkError(Exception):
    pass


class ClientError(Exception):  # botocore.exceptions.ClientError shape
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


def _tls_failure() -> Exception:
    try:
        try:
            raise ssl.SSLCertVerificationError(1, "certificate verify failed")
        except ssl.SSLCertVerificationError as inner:
            raise ConnectError("tls handshake failed") from inner
    except ConnectError as exc:
        return exc


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TransientError("x", "m"), ErrorClass.TRANSIENT),
        (DataError("x", "m"), ErrorClass.DATA),
        (SecurityError("x", "m"), ErrorClass.SECURITY),
        (SSHException("reset"), ErrorClass.TRANSIENT),
        (BadHostKeyException("changed"), ErrorClass.SECURITY),
        (AuthenticationException("denied"), ErrorClass.CONFIG),
        (TimeoutError(), ErrorClass.TRANSIENT),
        (ConnectionResetError(), ErrorClass.TRANSIENT),
        (NetworkError(), ErrorClass.TRANSIENT),
        (ClientError("SlowDown", 503), ErrorClass.TRANSIENT),
        (ClientError("AccessDenied", 403), ErrorClass.CONFIG),
        (ClientError("Whatever", 500), ErrorClass.TRANSIENT),
        (ClientError("Whatever", 404), ErrorClass.CONFIG),
        (ValueError("bug"), ErrorClass.INTERNAL),
    ],
)
def test_classify(exc: BaseException, expected: ErrorClass) -> None:
    assert classify(exc) is expected


def test_tls_verification_failure_is_security_even_when_wrapped() -> None:
    assert classify(_tls_failure()) is ErrorClass.SECURITY


@pytest.mark.parametrize(
    ("status", "expected"),
    [(408, "transient"), (429, "transient"), (503, "transient"), (401, "config"), (404, "config"), (302, "internal")],
)
def test_classify_http_status(status: int, expected: str) -> None:
    assert classify_http_status(status) == expected


# ---------------------------------------------------------------- retry: behaviour


def test_retry_after_parsing() -> None:
    now = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("Tue, 13 Oct 2026 12:00:30 GMT", now=now) == 30.0
    assert parse_retry_after("Tue, 13 Oct 2026 11:00:00 GMT", now=now) == 0.0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


def test_full_jitter_bounds() -> None:
    rng = random.Random(1)
    for attempt in range(1, 10):
        d = full_jitter(attempt, 1.0, 8.0, rng)
        assert 0.0 <= d <= min(8.0, 2 ** (attempt - 1))


def test_retries_transient_then_succeeds() -> None:
    sleeps: list[float] = []
    calls: list[int] = []

    def fn(attempt: int) -> str:
        calls.append(attempt)
        if attempt < 3:
            raise ConnectionResetError()
        return "ok"

    out = call_with_retry(fn, policy=Backoff(5, 1, 30), sleep=sleeps.append, rng=random.Random(0))
    assert out == "ok"
    assert calls == [1, 2, 3]
    assert len(sleeps) == 2


def test_non_transient_is_raised_at_once() -> None:
    calls: list[int] = []

    def fn(attempt: int) -> None:
        calls.append(attempt)
        raise ConfigError("missing_secret", "no such secret")

    with pytest.raises(ConfigError):
        call_with_retry(fn, policy=Backoff(5, 1, 30), sleep=lambda s: None)
    assert calls == [1]


def test_exhausted_attempts_raise_transient_with_cause() -> None:
    def fn(attempt: int) -> None:
        raise TimeoutError()

    with pytest.raises(TransientError) as ei:
        call_with_retry(fn, policy=Backoff(3, 0.01, 0.01), sleep=lambda s: None)
    assert ei.value.code == "retries_exhausted"
    assert isinstance(ei.value.__cause__, TimeoutError)


def test_retry_after_wins_and_deadline_stops() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    def fn(attempt: int) -> None:
        raise ConnectionResetError()

    with pytest.raises(TransientError) as ei:
        call_with_retry(
            fn,
            policy=Backoff(10, 1, 2, deadline=25),
            retry_after=lambda exc: 10.0,
            sleep=sleep,
            clock=lambda: now[0],
        )
    assert sleeps == [10.0, 10.0]  # a third wait would pass the 25 s deadline
    assert "deadline" in ei.value.message


# ---------------------------------------------------------------- jsonlog


def test_redactor_masks_values_lines_and_patterns() -> None:
    r = Redactor()
    pem = "\n".join(  # synthetic, not a usable key
        [
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAE",
            "AAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAg",
            "-----END OPENSSH PRIVATE KEY-----",
        ]
    )
    r.register("S3cr3t-Value-42")
    r.register(pem)
    r.register("abc")  # too short to register safely
    assert r.redact("token S3cr3t-Value-42 used") == f"token {MASK} used"
    assert "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAE" not in r.redact("line: b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAE")
    assert "abc" in r.redact("abc")
    assert r.redact("key AKIAIOSFODNN7EXAMPLE here") == f"key {MASK} here"
    assert r.redact("password: hunter2-synthetic") == f"password: {MASK}"
    assert r.redact("Authorization: Bearer abcdefghijklmnop") == f"Authorization: Bearer {MASK}"


def test_json_escaped_secret_is_masked_in_json_lines() -> None:
    buf = io.StringIO()
    log = JsonLogger(buf, clock=lambda: 0.0)
    secret = 'pa"ss\\word-synthetic'
    register_secret(secret)
    log.event("x", detail=f"value={secret}")
    line = buf.getvalue()
    assert json.dumps(secret)[1:-1] not in line  # the JSON-escaped form is what appears in a JSON line
    assert json.loads(line)["detail"] == f"value={MASK}"
    jsonlog.REDACTOR.clear()


def test_bound_fields_are_per_context_and_thread() -> None:
    buf = io.StringIO()
    log = JsonLogger(buf, clock=lambda: 0.0, static={"team": "clinops_data"})
    with bind(task_id="t1", run_key="abc123"):
        log.event("rule_end", rule_id="r_zip", bytes_out=10)

    def other() -> None:
        log.event("other_thread")

    with bind(task_id="t2"):
        th = threading.Thread(target=other)
        th.start()
        th.join()
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert lines[0] == {
        "ts": "1970-01-01T00:00:00.000Z",
        "level": "INFO",
        "event": "rule_end",
        "team": "clinops_data",
        "task_id": "t1",
        "run_key": "abc123",
        "rule_id": "r_zip",
        "bytes_out": 10,
    }
    assert "task_id" not in lines[1]  # contextvars are not inherited by a plain new thread


def test_metric_is_embedded_metric_format() -> None:
    buf = io.StringIO()
    JsonLogger(buf, clock=lambda: 1.5).metric("BytesTransferred", 42, "Bytes", pipeline_id="p1")
    doc = json.loads(buf.getvalue())
    assert doc["_aws"]["Timestamp"] == 1500
    assert doc["_aws"]["CloudWatchMetrics"][0]["Metrics"] == [{"Name": "BytesTransferred", "Unit": "Bytes"}]
    assert doc["BytesTransferred"] == 42
    assert doc["pipeline_id"] == "p1"


def test_configure_bridges_and_clamps_library_loggers() -> None:
    buf = io.StringIO()
    configure(buf, service="factory-runtime")
    try:
        register_secret("bridge-secret-value-1")
        logging.getLogger("botocore").debug("debug bridge-secret-value-1")  # clamped: not emitted
        logging.getLogger("botocore").warning("warn bridge-secret-value-1")
        lines = [json.loads(x) for x in buf.getvalue().splitlines()]
        assert [x["level"] for x in lines] == ["WARNING"]
        assert lines[0]["library"] == "botocore"
        assert "bridge-secret-value-1" not in buf.getvalue()
        assert get_logger().static == {"service": "factory-runtime"}
    finally:
        configure(None)
        jsonlog.REDACTOR.clear()


# ---------------------------------------------------------------- runkey

_AIRFLOW_RUN_ID = re.compile(r"^[A-Za-z0-9_.~:+-]+$")


@given(st.text(min_size=1, max_size=400))
def test_run_dir_is_safe_and_unique(key: str) -> None:
    d = run_dir(key)
    assert _AIRFLOW_RUN_ID.match(d)
    assert len(d) <= 213
    assert d.endswith(run_key_hash(key))
    assert "/" not in d
    assert not d.startswith((".", "-", "_"))


def test_run_dir_examples() -> None:
    assert run_dir("REQ-12345678").startswith("REQ-12345678-")
    assert run_dir("a/b") != run_dir("a_b")
    assert run_dir("../../etc").startswith("etc-")
    with pytest.raises(ValueError):
        run_dir("")


# ---------------------------------------------------------------- globmatch


@pytest.mark.parametrize(
    ("pattern", "path", "ok"),
    [
        ("**/*", "a.txt", True),
        ("**/*", "x/y/a.txt", True),
        ("**/.DS_Store", ".DS_Store", True),
        ("**/.DS_Store", "a/b/.DS_Store", True),
        ("**/.DS_Store", "a/b/x.DS_Store", False),
        ("*.csv", "a.csv", True),
        ("*.csv", "d/a.csv", False),
        ("data/**", "data/x/y", True),
        ("data/**/*.csv", "data/a.csv", True),
        ("data/**/*.csv", "data/1/2/a.csv", True),
        ("file?.txt", "file1.txt", True),
        ("file?.txt", "file/.txt", False),
        ("[ab]*.txt", "b1.txt", True),
        ("[!ab]*.txt", "c1.txt", True),
        ("[!ab]*.txt", "a1.txt", False),
        ("[0-9].txt", "7.txt", True),
        ("a.b", "aXb", False),
    ],
)
def test_glob_match(pattern: str, path: str, ok: bool) -> None:
    assert glob_match(pattern, path) is ok


@pytest.mark.parametrize("bad", ["", "/abs/*", "a\\b", "a**b", "a//b", "[abc", "[]"])
def test_glob_rejects(bad: str) -> None:
    with pytest.raises(GlobError):
        glob_match(bad, "x")


# ---------------------------------------------------------------- cel.evaluate_value


def test_evaluate_value() -> None:
    act: dict[str, Any] = {"vars": {"study_id": "ONC101", "n": 7}}
    assert cel.evaluate_value("vars.study_id + '_x.zip'", act) == "ONC101_x.zip"
    assert cel.evaluate_value("vars.n * 2", act) == 14
    assert cel.evaluate_value("[vars.n, 1]", act) == [7, 1]
    with pytest.raises(cel.CelEvalError):
        cel.evaluate_value("vars.missing + 1", act)
