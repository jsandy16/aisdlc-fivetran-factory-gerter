"""HTTP client for the self-service portal and named REST APIs (portal-integration.md §2, ADR-0009).

Per call: authentication (OAuth2 client credentials with a cached token and one refresh on 401, API key, or mTLS),
a per-process circuit breaker and token-bucket rate limit, retries with full jitter that honour Retry-After, strict
response validation against the spec's field specs, pagination with a hard page cap, size-capped bodies, and one audit
event per call (status, latency, bytes, response sha256; never the payload). All TLS rules come from tls.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import jmespath
from factory_core.errors import ConfigError, ErrorClass, SecurityError, TransientError
from factory_core.retry import Backoff, call_with_retry, classify, classify_http_status, parse_retry_after
from factory_core.templates import parse_template, render
from factory_spec.dynamic import record_model
from pydantic import BaseModel, ValidationError

from .secrets.value import SecretValue
from .tls import http_client, read_capped, ssl_context

PAGE_CAP = 1000
MAX_BODY = 16 * 1024 * 1024
_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')

# ---------------------------------------------------------------- breaker and rate limit (per process, per base URL)


class CircuitBreaker:
    def __init__(self, threshold: int, reset_after: float, clock: Callable[[], float]) -> None:
        self.threshold = threshold
        self.reset_after = reset_after
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None
        self._lock = threading.Lock()

    def before(self) -> None:
        with self._lock:
            if self.opened_at is not None:
                if self.clock() - self.opened_at < self.reset_after:
                    raise TransientError("circuit_open", "too many recent failures; the call was not attempted")
                self.opened_at = None  # half-open: let one call through
                self.failures = self.threshold - 1

    def success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = self.clock()


class RateLimiter:
    def __init__(self, per_second: int, clock: Callable[[], float], sleep: Callable[[float], Any]) -> None:
        self.interval = 1.0 / max(1, per_second)
        self.clock = clock
        self.sleep = sleep
        self.next_at = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self.clock()
            wait = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if wait > 0:
            self.sleep(wait)


_BREAKERS: dict[str, CircuitBreaker] = {}
_LIMITERS: dict[str, RateLimiter] = {}
_REG_LOCK = threading.Lock()


def reset_process_state() -> None:
    """Tests: forget breakers and limiters (they are per process by design)."""
    with _REG_LOCK:
        _BREAKERS.clear()
        _LIMITERS.clear()


# ---------------------------------------------------------------- client


@dataclass(frozen=True)
class Page:
    status: int
    body: bytes
    data: Any  # validated and dumped by alias, or None for non-JSON formats
    number: int
    next_link: str | None = None  # RFC 8288 Link rel="next", for link_header pagination


class _RetryableStatusError(Exception):
    def __init__(self, status: int, retry_after: float | None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


class ApiClient:
    def __init__(
        self,
        api: Any,
        base_url: str,
        *,
        name: str,
        secret: Callable[..., SecretValue],
        extra_ca: bytes | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = time.sleep,
        on_call: Callable[..., None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.api = api
        self.base_url = base_url.rstrip("/")
        self.name = name
        self.secret = secret
        self.clock = clock
        self.sleep = sleep
        self.on_call = on_call or (lambda **kw: None)
        self.rng = rng or random.Random()  # noqa: S311 - jitter only
        rel = api.reliability
        auth = api.auth
        client_cert = None
        pinned_ca = None
        if auth.type == "mtls":
            client_cert = (secret(auth.cert, purpose=f"{name} mTLS"), secret(auth.key, purpose=f"{name} mTLS"))
            if auth.ca:
                with secret(auth.ca, purpose=f"{name} CA").reveal() as pem:
                    pinned_ca = pem
        ctx = ssl_context(pinned_ca=pinned_ca, extra_ca=extra_ca, client_cert=client_cert)
        self.http = http_client(
            ctx, connect_timeout=rel.connect_timeout, read_timeout=rel.read_timeout, transport=transport
        )
        self._token: SecretValue | None = None
        self._token_expires = 0.0
        with _REG_LOCK:
            self.breaker = _BREAKERS.setdefault(
                self.base_url, CircuitBreaker(rel.breaker_failure_threshold, rel.breaker_reset_after, clock)
            )
            self.limiter = _LIMITERS.setdefault(self.base_url, RateLimiter(rel.rate_limit_per_second, clock, sleep))

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------ auth

    def _fetch_token(self, refresh_secret: bool) -> None:
        auth = self.api.auth
        creds = self.secret(auth.client, purpose=f"{self.name} OAuth client", refresh=refresh_secret).json_fields()
        if "client_id" not in creds or "client_secret" not in creds:
            raise ConfigError("secret_format_invalid", "the OAuth client secret needs client_id and client_secret")
        with creds["client_id"].reveal() as cid, creds["client_secret"].reveal() as csec:
            basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
            SecretValue(basic, "basic_auth")  # register the encoded form with the redactor
            form = {"grant_type": "client_credentials"}
            if auth.scopes:
                form["scope"] = " ".join(auth.scopes)
            try:
                res = self.http.post(
                    self.base_url + auth.token_path,
                    data=form,
                    headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
                )
            except httpx.TransportError as exc:
                raise self._transport_error(exc) from exc
        if 300 <= res.status_code < 400:
            raise ConfigError("redirect_refused", f"the {self.name} token endpoint redirected; not followed")
        if res.status_code in (400, 401, 403):
            if not refresh_secret:  # the client secret may have been rotated: fetch it again once
                self._fetch_token(True)
                return
            raise ConfigError("auth_refused", f"{self.name} refused the client credentials ({res.status_code})")
        if res.status_code >= 400:
            raise TransientError("auth_unavailable", f"{self.name} token endpoint failed ({res.status_code})")
        try:
            doc = res.json()
            token = str(doc["access_token"])
            expires = int(doc.get("expires_in", 300))
        except (ValueError, KeyError, TypeError):
            raise ConfigError("auth_response_invalid", f"{self.name} returned no access token") from None
        self._token = SecretValue(token, f"{self.name}_access_token")
        self._token_expires = self.clock() + max(0, expires - 30)

    def _auth(self, headers: dict[str, str], params: dict[str, str], force: bool) -> None:
        auth = self.api.auth
        if auth.type == "oauth2_client_credentials":
            if force or self._token is None or self.clock() >= self._token_expires:
                self._fetch_token(False)
            if self._token is None:  # pragma: no cover - _fetch_token sets it or raises
                raise ConfigError("auth_refused", f"{self.name}: no access token")
            with self._token.reveal() as t:
                headers["Authorization"] = f"Bearer {t}"
        elif auth.type == "api_key":
            with self.secret(auth.secret, purpose=f"{self.name} API key").reveal() as k:
                if auth.send_in == "header":
                    headers[auth.name] = k
                else:
                    params[auth.name] = k

    # ------------------------------------------------------------ one request (retries, breaker, rate limit)

    def _transport_error(self, exc: Exception) -> Exception:
        cls = classify(exc)
        if cls is ErrorClass.SECURITY:
            return SecurityError("tls_untrusted", f"{self.name}: the server certificate is not trusted")
        if cls is ErrorClass.CONFIG:
            return ConfigError("tls_client_rejected", f"{self.name}: the server rejected our client certificate")
        return exc

    def _once(self, method: str, url: str, params: dict[str, str], endpoint_id: str, path_t: str, page: int) -> Page:
        rel = self.api.reliability

        def attempt(n: int) -> tuple[int, bytes, httpx.Headers]:
            self.breaker.before()
            self.limiter.acquire()
            headers: dict[str, str] = {"Accept": "application/json"}
            q = dict(params)
            self._auth(headers, q, force=False)
            t0 = time.monotonic()
            try:
                status, body, hdrs = self._send(method, url, headers, q)
                if status == 401 and self.api.auth.type == "oauth2_client_credentials":
                    headers = {"Accept": "application/json"}
                    self._auth(headers, q, force=True)  # token expired early: refresh once
                    status, body, hdrs = self._send(method, url, headers, q)
            except httpx.TransportError as exc:
                self.breaker.failure()
                raise self._transport_error(exc) from exc
            ms = int((time.monotonic() - t0) * 1000)
            self.on_call(
                endpoint_id=endpoint_id,
                method=method,
                path_template=path_t,
                status=status,
                latency_ms=ms,
                attempt=n,
                page=page,
                bytes=len(body),
                response_sha256=hashlib.sha256(body).hexdigest(),
            )
            if status in rel.retry_on_status:
                self.breaker.failure()
                hint = parse_retry_after(hdrs.get("Retry-After")) if rel.honour_retry_after else None
                raise _RetryableStatusError(status, hint)
            self.breaker.success()
            return status, body, hdrs

        def classify_attempt(exc: BaseException) -> ErrorClass:
            return ErrorClass.TRANSIENT if isinstance(exc, _RetryableStatusError) else classify(exc)

        try:
            status, body, hdrs = call_with_retry(
                attempt,
                policy=Backoff(rel.max_attempts, rel.backoff_base, rel.backoff_max),
                classify_exc=classify_attempt,
                retry_after=lambda e: e.retry_after if isinstance(e, _RetryableStatusError) else None,
                sleep=self.sleep,
                clock=self.clock,
                rng=self.rng,
            )
        except TransientError as exc:
            if isinstance(exc.__cause__, _RetryableStatusError):
                raise TransientError(
                    "api_unavailable", f"{self.name} kept answering {exc.__cause__.status} after retries"
                ) from None
            raise
        if 300 <= status < 400:
            raise ConfigError("redirect_refused", f"{self.name} answered with a redirect ({status}); not followed")
        if status >= 400:
            cls = classify_http_status(status)
            if cls is ErrorClass.TRANSIENT:
                raise TransientError("api_unavailable", f"{self.name} failed ({status})")
            code = "auth_refused" if status in (401, 403) else "api_request_failed"
            raise ConfigError(code, f"{self.name} {endpoint_id} answered {status}")
        m = _LINK_NEXT.search(hdrs.get("Link", ""))
        return Page(status, body, None, page, m.group(1) if m else None)

    def _send(
        self, method: str, url: str, headers: dict[str, str], params: dict[str, str]
    ) -> tuple[int, bytes, httpx.Headers]:
        with self.http.stream(method, url, headers=headers, params=params or None) as res:
            return res.status_code, read_capped(res, MAX_BODY), res.headers

    # ------------------------------------------------------------ endpoints and pagination

    def _url(self, endpoint: Any, values: dict[str, str]) -> tuple[str, dict[str, str]]:
        try:
            path = render(parse_template(endpoint.path), {k: quote(v, safe="") for k, v in values.items()})
            query = {k: render(parse_template(v), values) for k, v in endpoint.query.items()}
        except Exception:
            raise ConfigError("api_path_unrenderable", f"{self.name} {endpoint.id}: a path value is missing") from None
        return self.base_url + path, query

    def _validate(self, endpoint: Any, model: type[BaseModel] | None, body: bytes) -> Any:
        if model is None:
            return None
        try:
            obj = model.model_validate_json(body)
        except ValidationError as exc:
            where = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors(include_input=False)})[:5]
            raise ConfigError(
                "api_response_invalid", f"{self.name} {endpoint.id}: the response does not match the spec at {where}"
            ) from None
        return obj.model_dump(mode="json", by_alias=True)

    def pages(self, endpoint: Any, values: dict[str, str], *, validate: bool = True) -> Iterator[Page]:
        url, query = self._url(endpoint, values)
        model = record_model(f"{self.name}_{endpoint.id}", endpoint.response) if validate else None
        pag = endpoint.pagination
        seen_cursors: set[str] = set()
        seen_links: set[str] = set()
        url_is_base = True
        page_no = 1
        base = urlsplit(self.base_url)
        origin = (base.scheme, base.netloc)
        while True:
            if page_no > PAGE_CAP:
                raise ConfigError("pagination_limit", f"{self.name} {endpoint.id}: more than {PAGE_CAP} pages")
            params = dict(query)
            if pag.type == "page":
                params[pag.page_param] = str(page_no)
                params[pag.size_param] = str(pag.size)
            raw = self._once(endpoint.method, url, params if url_is_base else {}, endpoint.id, endpoint.path, page_no)
            data = self._validate(endpoint, model, raw.body)
            yield Page(raw.status, raw.body, data, page_no)
            if pag.type == "none":
                return
            if pag.type == "page":
                lists = [v for v in (data or {}).values() if isinstance(v, list)] if isinstance(data, dict) else []
                if not lists or all(len(v) == 0 for v in lists):
                    return
            elif pag.type == "cursor":
                try:  # paging metadata is protocol, not content: read it from the raw body, not the validated data
                    nxt = jmespath.search(pag.next, json.loads(raw.body))
                except ValueError:
                    raise ConfigError(
                        "api_response_invalid", f"{self.name} {endpoint.id}: the page is not JSON"
                    ) from None
                if not nxt:
                    return
                if str(nxt) in seen_cursors:
                    raise ConfigError("pagination_loop", f"{self.name} {endpoint.id}: the cursor repeats")
                seen_cursors.add(str(nxt))
                query = {**query, pag.cursor_param: str(nxt)}
            elif pag.type == "link_header":
                if not raw.next_link:
                    return
                nxt = httpx.URL(url).join(raw.next_link)
                if (nxt.scheme, nxt.netloc.decode()) != origin:
                    raise SecurityError("pagination_link_offsite", f"{self.name} {endpoint.id}: next page is elsewhere")
                if str(nxt) in seen_links:
                    raise ConfigError("pagination_loop", f"{self.name} {endpoint.id}: the next link repeats")
                seen_links.add(str(nxt))
                url, url_is_base = str(nxt), False
            page_no += 1


def merged(pages: list[Page]) -> Any:
    """Several pages of one answer: top-level lists concatenated, other fields from the first page."""
    if not pages:
        return None
    first = pages[0].data
    if len(pages) == 1 or not isinstance(first, dict):
        return first
    out = dict(first)
    for p in pages[1:]:
        if isinstance(p.data, dict):
            for k, v in p.data.items():
                if isinstance(v, list) and isinstance(out.get(k), list):
                    out[k] = [*out[k], *v]
    return out
