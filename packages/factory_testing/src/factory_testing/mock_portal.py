"""Mock self-service portal and mock REST API (TLS, OAuth2 client credentials or API key, fault injection).

The portal answers the worked example's `delivery_targets` endpoint from synthetic targets set by the test; faults
are queued and consumed one per API request: "429" (with Retry-After), "503", "drift" (wrong field types), "401"
(token expired early), "redirect", "oversize", "wrong_study". `example_from_spec` builds a minimal valid response from
an endpoint's field specs. Synthetic data only.
"""

from __future__ import annotations

import base64
import re
import secrets
import time
from typing import Any

from .certs import TestCA
from .httpserver import Request, Response, StandIn

_PATH = re.compile(r"^/api/v1/studies/([^/]+)/delivery-targets$")


class MockPortal(StandIn):
    def __init__(
        self,
        ca: TestCA,
        hostname: str = "portal.dev.acme.example",
        *,
        client_id: str,
        client_secret: str,
        token_ttl: int = 3600,
        require_client_cert: bool = False,
    ) -> None:
        super().__init__(ca, (hostname,), require_client_cert=require_client_cert)
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_ttl = token_ttl
        self.tokens: dict[str, float] = {}
        self.targets: dict[str, list[dict[str, Any]]] = {}
        self.faults: list[str] = []
        self.token_requests = 0

    def set_targets(self, study_id: str, x: str | None, y: str | None, *, active: bool = True) -> None:
        t = []
        if x is not None:
            t.append({"slot": "x", "uri": x, "active": active})
        if y is not None:
            t.append({"slot": "y", "uri": y, "active": active})
        self.targets[study_id] = t

    def _token(self, req: Request) -> Response:
        self.token_requests += 1
        expected = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        if req.headers.get("authorization") != f"Basic {expected}":
            return Response(401, {"error": "invalid_client"})
        token = secrets.token_urlsafe(24)
        self.tokens[token] = time.time() + self.token_ttl
        return Response(200, {"access_token": token, "token_type": "Bearer", "expires_in": self.token_ttl})

    def _authorised(self, req: Request) -> bool:
        auth = req.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        return auth.startswith("Bearer ") and self.tokens.get(token, 0) > time.time()

    def handle(self, req: Request) -> Response:
        if req.method == "POST" and req.path == "/oauth2/token":
            return self._token(req)
        if not self._authorised(req):
            return Response(401, {"error": "invalid_token"})
        fault = self.faults.pop(0) if self.faults else None
        if fault == "429":
            return Response(429, {"error": "slow down"}, {"Retry-After": "1"})
        if fault == "503":
            return Response(503, {"error": "unavailable"})
        if fault == "401":
            self.tokens.clear()
            return Response(401, {"error": "invalid_token"})
        if fault == "redirect":
            return Response(302, b"", {"Location": "https://evil.example.com/"})
        if fault == "oversize":
            return Response(200, b"[" + b"0," * (20 * 1024 * 1024) + b"0]")
        m = _PATH.match(req.path)
        if req.method != "GET" or m is None:
            return Response(404, {"error": "not found"})
        study = m.group(1)
        if fault == "drift":
            return Response(200, {"study_id": 101, "targets": "x"})
        answered = "ONC999" if fault == "wrong_study" else study
        return Response(200, {"study_id": answered, "targets": self.targets.get(study, [])})


class MockApi(StandIn):
    """A paginated items API (cursor in `meta.next_cursor`), API key in X-Api-Key."""

    def __init__(self, ca: TestCA, hostname: str, *, api_key: str, pages: list[list[dict[str, Any]]]) -> None:
        super().__init__(ca, (hostname,))
        self.api_key = api_key
        self.page_data = pages

    def handle(self, req: Request) -> Response:
        if req.headers.get("x-api-key") != self.api_key:
            return Response(401, {"error": "bad key"})
        if req.path != "/v1/items":
            return Response(404, {})
        n = int(req.query.get("cursor", "0"))
        nxt = str(n + 1) if n + 1 < len(self.page_data) else None
        return Response(200, {"items": self.page_data[n], "meta": {"next_cursor": nxt}})


def example_from_spec(record: Any) -> dict[str, Any]:
    """A minimal valid instance of a RecordSpec (synthetic values), for contract tests of arbitrary endpoints."""

    def value(spec: Any) -> Any:
        if spec.type == "string":
            return "x" * max(1, min(3, spec.max_length or 3)) if spec.pattern is None else _from_pattern(spec.pattern)
        if spec.type == "integer":
            return spec.minimum if spec.minimum is not None else 0
        if spec.type == "boolean":
            return True
        if spec.type == "enum":
            return spec.values[0]
        if spec.type == "list":
            return [value(spec.items)]
        return {k: value(v) for k, v in spec.fields.items() if v.required}

    return {k: value(v) for k, v in record.fields.items() if v.required}


def _from_pattern(pattern: str) -> str:
    known = {
        r"^[A-Z]{3}[0-9]{3,5}$": "ONC101",
        r"^PKG[0-9]{4,8}$": "PKG0042",
        r"^[A-Za-z0-9-]{8,64}$": "REQ-00000001",
    }
    if pattern in known:
        return known[pattern]
    raise ValueError(f"no synthetic example for pattern {pattern}")
