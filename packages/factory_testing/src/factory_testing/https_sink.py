"""A file-upload HTTPS endpoint for tests: PUT/POST store a file, HEAD/GET describe or return it.

Switches cover the evidence levels of decision P3-3: `digest` (HEAD/GET carry Repr-Digest), `allow_get` (GET
read-back), `allow_head` (HEAD at all). `honour_if_none_match` answers 412 for an existing file; `fail_puts` answers
503 that many times first; `corrupt` stores a different byte than it received. An upload whose body is shorter than
its Content-Length (the client aborted) is discarded, as a real server would. Auth: bearer token and/or mTLS.
"""

from __future__ import annotations

import base64
import hashlib

from .certs import TestCA
from .httpserver import Request, Response, StandIn


class HttpsSink(StandIn):
    def __init__(
        self,
        ca: TestCA,
        hostname: str,
        *,
        token: str | None = None,
        require_client_cert: bool = False,
        digest: bool = True,
        allow_get: bool = True,
        allow_head: bool = True,
        honour_if_none_match: bool = True,
    ) -> None:
        super().__init__(ca, (hostname,), require_client_cert=require_client_cert)
        self.token = token
        self.digest = digest
        self.allow_get = allow_get
        self.allow_head = allow_head
        self.honour_if_none_match = honour_if_none_match
        self.fail_puts = 0
        self.corrupt = False
        self.files: dict[str, bytes] = {}

    def _headers(self, data: bytes) -> dict[str, str]:
        if not self.digest:
            return {}
        return {"Repr-Digest": f"sha-256=:{base64.b64encode(hashlib.sha256(data).digest()).decode()}:"}

    def handle(self, req: Request) -> Response:
        if self.token is not None and req.headers.get("authorization") != f"Bearer {self.token}":
            return Response(401)
        data = self.files.get(req.path)
        if req.method == "HEAD":
            if not self.allow_head:
                return Response(405)
            return Response(404) if data is None else Response(200, data, self._headers(data))
        if req.method == "GET":
            if not self.allow_get:
                return Response(405)
            return Response(404) if data is None else Response(200, data, self._headers(data))
        if req.method in ("PUT", "POST"):
            if self.fail_puts > 0:
                self.fail_puts -= 1
                return Response(503)
            if req.headers.get("if-none-match") == "*" and data is not None and self.honour_if_none_match:
                return Response(412)
            if len(req.body) != int(req.headers.get("content-length") or 0):
                return Response(400)
            body = req.body
            if self.corrupt and body:
                body = bytes([body[0] ^ 0xFF]) + body[1:]
            self.files[req.path] = body
            return Response(201)
        return Response(405)
