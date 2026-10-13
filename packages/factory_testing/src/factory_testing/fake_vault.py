"""A fake Vault for tests: AWS IAM login, KV v2 with versions, token renewal, health, and failure modes.

It checks what a real Vault would check from the client's side of the protocol: the login carries a SigV4-signed STS
GetCallerIdentity request (Authorization header present, server-id header signed and equal to the configured value),
the role is the expected one, tokens expire, the namespace header matches. It cannot verify the AWS signature (that
would call STS); a real Vault does. Synthetic secrets only.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any

from .certs import TestCA
from .httpserver import Request, Response, StandIn


class FakeVault(StandIn):
    def __init__(
        self,
        ca: TestCA,
        hostname: str = "vault.dev.acme.example",
        *,
        role: str,
        server_id: str | None,
        namespace: str | None = None,
        auth_mount: str = "aws",
        kv_mount: str = "kv",
        ttl: int = 3600,
    ) -> None:
        super().__init__(ca, (hostname,))
        self.role = role
        self.server_id = server_id
        self.namespace = namespace
        self.auth_mount = auth_mount
        self.kv_mount = kv_mount
        self.ttl = ttl
        self.mode = "ok"  # ok | sealed | forbidden | expire_tokens
        self.tokens: dict[str, float] = {}
        self.kv: dict[str, list[dict[str, Any] | None]] = {}  # path -> versions (None = deleted)
        self.logins = 0

    def put(self, path: str, data: dict[str, Any]) -> int:
        self.kv.setdefault(path, []).append(dict(data))
        return len(self.kv[path])

    def delete_version(self, path: str, version: int) -> None:
        self.kv[path][version - 1] = None

    def _token_ok(self, req: Request) -> bool:
        t = req.headers.get("x-vault-token")
        if t is None or t not in self.tokens:
            return False
        return self.mode != "expire_tokens" and time.time() < self.tokens[t]

    def _login(self, req: Request) -> Response:
        doc = req.json()
        if doc.get("role") != self.role:
            return Response(400, {"errors": ["invalid role"]})
        try:
            headers = json.loads(base64.b64decode(doc["iam_request_headers"]))
            url = base64.b64decode(doc["iam_request_url"]).decode()
            body = base64.b64decode(doc["iam_request_body"]).decode()
        except (KeyError, ValueError):
            return Response(400, {"errors": ["malformed login"]})
        lower = {k.lower(): v[0] if isinstance(v, list) else v for k, v in headers.items()}
        auth = str(lower.get("authorization", ""))
        if not auth.startswith("AWS4-HMAC-SHA256 ") or "GetCallerIdentity" not in body or "sts." not in url:
            return Response(400, {"errors": ["not a signed GetCallerIdentity request"]})
        if self.server_id is not None:
            signed = auth.split("SignedHeaders=")[1].split(",")[0].split(";") if "SignedHeaders=" in auth else []
            if lower.get("x-vault-aws-iam-server-id") != self.server_id or "x-vault-aws-iam-server-id" not in signed:
                return Response(400, {"errors": ["expected server id header is missing or not signed"]})
        token = "hvs." + secrets.token_urlsafe(24)
        self.tokens[token] = time.time() + self.ttl
        self.logins += 1
        return Response(200, {"auth": {"client_token": token, "lease_duration": self.ttl, "renewable": True}})

    def handle(self, req: Request) -> Response:
        if self.mode == "sealed":
            return Response(503, {"errors": ["Vault is sealed"]})
        if (
            self.namespace is not None
            and req.path != "/v1/sys/health"
            and req.headers.get("x-vault-namespace") != self.namespace
        ):
            return Response(403, {"errors": ["permission denied"]})
        if req.path == "/v1/sys/health":
            return Response(200, {"initialized": True, "sealed": False})
        if req.method == "POST" and req.path == f"/v1/auth/{self.auth_mount}/login":
            return self._login(req)
        if self.mode == "forbidden" or not self._token_ok(req):
            return Response(403, {"errors": ["permission denied"]})
        if req.method == "POST" and req.path == "/v1/auth/token/renew-self":
            token = req.headers["x-vault-token"]
            self.tokens[token] = time.time() + self.ttl
            return Response(200, {"auth": {"client_token": token, "lease_duration": self.ttl}})
        prefix = f"/v1/{self.kv_mount}/data/"
        if req.method == "GET" and req.path.startswith(prefix):
            versions = self.kv.get(req.path[len(prefix) :])
            if not versions:
                return Response(404, {"errors": []})
            n = int(req.query.get("version", len(versions)))
            if n < 1 or n > len(versions):
                return Response(404, {"errors": []})
            data = versions[n - 1]
            meta = {
                "version": n,
                "deletion_time": "" if data is not None else "2026-10-01T00:00:00Z",
                "destroyed": False,
            }
            return Response(200, {"data": {"data": data, "metadata": meta}})
        return Response(404, {"errors": ["no handler"]})
