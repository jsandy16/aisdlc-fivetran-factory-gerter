"""Retries with full jitter, Retry-After and an overall deadline, plus exception classification.

Adapted from pipeline-factory 0.1.0 `pf_runtime/retry.py` (full jitter, classification by exception class name so this
module imports no client library). Added: Retry-After (seconds or HTTP-date), a deadline, HTTP status classification,
and TLS verification failures classified as `security`, never as transient.

Only `transient` failures are retried; every other class surfaces at once. Sleep, clock and randomness are injectable
so tests are deterministic.
"""

from __future__ import annotations

import random
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

from .errors import ErrorClass, FactoryError, TransientError

T = TypeVar("T")

# exception class names (anywhere in the MRO) of client libraries, so no client library is imported here
_TRANSIENT_NAMES = frozenset(
    {
        # paramiko
        "SSHException",
        "ChannelException",
        "NoValidConnectionsError",
        # botocore
        "EndpointConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ConnectionClosedError",
        # httpx (TransportError family; TLS failures are classified first, see classify)
        "TimeoutException",
        "NetworkError",
        "RemoteProtocolError",
        "ProxyError",
        # stdlib
        "TimeoutError",
        "ConnectionError",
        "EOFError",
    }
)
_SECURITY_NAMES = frozenset({"BadHostKeyException", "SSLCertVerificationError", "CertificateError"})
_CONFIG_NAMES = frozenset({"AuthenticationException", "PasswordRequiredException", "NoCredentialsError"})
# TLS alerts that mean "the server rejected our client certificate" (mTLS): a configuration problem, not a blip
TLS_CLIENT_REJECTED = frozenset(
    {
        "TLSV13_ALERT_CERTIFICATE_REQUIRED",
        "TLSV1_ALERT_UNKNOWN_CA",
        "TLSV1_ALERT_ACCESS_DENIED",
        "SSLV3_ALERT_BAD_CERTIFICATE",
        "SSLV3_ALERT_CERTIFICATE_UNKNOWN",
        "SSLV3_ALERT_CERTIFICATE_EXPIRED",
        "SSLV3_ALERT_CERTIFICATE_REVOKED",
    }
)

_AWS_TRANSIENT_CODES = frozenset(
    {
        "SlowDown",
        "Throttling",
        "ThrottlingException",
        "ThrottledException",
        "RequestTimeout",
        "RequestTimeoutException",
        "ProvisionedThroughputExceededException",
        "RequestLimitExceeded",
        "InternalError",
        "InternalFailure",
        "ServiceUnavailable",
        "TransactionInProgressException",
    }
)
_AWS_CONFIG_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "ExpiredToken",
        "ExpiredTokenException",
        "UnrecognizedClientException",
        "NoSuchBucket",
        "NoSuchKey",
        "ResourceNotFoundException",
        "KMS.NotFoundException",
        "KMS.DisabledException",
        "AuthorizationError",
    }
)


def classify_http_status(status: int) -> ErrorClass:
    """408, 425, 429 and 5xx are transient; other 4xx are configuration problems (auth, wrong path, bad request)."""
    if status in (408, 425, 429) or 500 <= status <= 599:
        return ErrorClass.TRANSIENT
    if 400 <= status <= 499:
        return ErrorClass.CONFIG
    return ErrorClass.INTERNAL


def _names(exc: BaseException) -> set[str]:
    return {c.__name__ for c in type(exc).__mro__}


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in out and len(out) < 16:
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def classify(exc: BaseException) -> ErrorClass:
    """The error class of any exception. TLS verification failures anywhere in the cause chain are `security`."""
    if isinstance(exc, FactoryError):
        return exc.error_class
    chain = _chain(exc)
    for e in chain:
        if isinstance(e, ssl.SSLCertVerificationError) or _names(e) & _SECURITY_NAMES:
            return ErrorClass.SECURITY
    for e in chain:
        if isinstance(e, ssl.SSLError) and getattr(e, "reason", None) in TLS_CLIENT_REJECTED:
            return ErrorClass.CONFIG
    names = _names(exc)
    if names & _CONFIG_NAMES:
        return ErrorClass.CONFIG
    response = getattr(exc, "response", None)
    if isinstance(response, dict) and "Error" in response:  # botocore ClientError
        code = str(response.get("Error", {}).get("Code", ""))
        status = int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code in _AWS_TRANSIENT_CODES:
            return ErrorClass.TRANSIENT
        if code in _AWS_CONFIG_CODES:
            return ErrorClass.CONFIG
        if status:
            return classify_http_status(status)
        return ErrorClass.INTERNAL
    if names & _TRANSIENT_NAMES or isinstance(exc, TimeoutError | ConnectionError | EOFError):
        return ErrorClass.TRANSIENT
    return ErrorClass.INTERNAL


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date), or None when absent or invalid."""
    if value is None:
        return None
    v = value.strip()
    if v.isdigit():
        return float(int(v))
    try:
        when = parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


@dataclass(frozen=True)
class Backoff:
    """attempts: total tries (>= 1). base/maximum: seconds for full jitter. deadline: overall seconds, or None."""

    attempts: int = 5
    base: float = 1.0
    maximum: float = 30.0
    deadline: float | None = None

    def __post_init__(self) -> None:
        if self.attempts < 1 or self.base < 0 or self.maximum < 0:
            raise ValueError("attempts must be >= 1 and delays >= 0")


def full_jitter(attempt: int, base: float, maximum: float, rng: random.Random) -> float:
    """AWS 'full jitter': uniform(0, min(maximum, base * 2**(attempt-1)))."""
    return rng.uniform(0.0, min(maximum, base * 2 ** (attempt - 1)))


def call_with_retry(
    fn: Callable[[int], T],
    *,
    policy: Backoff,
    classify_exc: Callable[[BaseException], ErrorClass] = classify,
    retry_after: Callable[[BaseException], float | None] = lambda exc: None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    rng: random.Random | None = None,
) -> T:
    """Call fn(attempt) until it returns, retrying transient failures only.

    A server-supplied Retry-After wins over the jittered delay. When the next wait would pass the deadline, or the
    attempts are used up, the last transient failure is raised (a non-FactoryError is wrapped in TransientError so
    the caller exits 75 with a stable code).
    """
    rng = rng or random.Random()  # noqa: S311 - jitter, not cryptography
    start = clock()
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(attempt)
        except BaseException as exc:
            if not isinstance(exc, Exception) or classify_exc(exc) is not ErrorClass.TRANSIENT:
                raise
            delay = full_jitter(attempt, policy.base, policy.maximum, rng)
            hinted = retry_after(exc)
            if hinted is not None:
                delay = hinted
            out_of_time = policy.deadline is not None and (clock() - start) + delay > policy.deadline
            if attempt >= policy.attempts or out_of_time:
                if isinstance(exc, FactoryError):
                    raise
                why = "deadline reached" if out_of_time else f"{attempt} attempts"
                raise TransientError("retries_exhausted", f"{type(exc).__name__} ({why})") from exc
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
