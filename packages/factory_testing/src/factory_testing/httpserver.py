"""A small threaded HTTPS server base for the stand-ins (mock portal, HTTPS sink, fake Vault, mock APIs)."""

from __future__ import annotations

import json
import ssl
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .certs import TestCA


class Request:
    def __init__(self, handler: BaseHTTPRequestHandler, body: bytes) -> None:
        parts = urlsplit(handler.path)
        self.method = handler.command
        self.path = parts.path
        self.query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.headers = {k.lower(): v for k, v in handler.headers.items()}
        self.body = body
        self.client_cert = handler.connection.getpeercert() if isinstance(handler.connection, ssl.SSLSocket) else None

    def json(self) -> Any:
        return json.loads(self.body or b"{}")


class Response:
    def __init__(self, status: int = 200, body: bytes | Any = b"", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = dict(headers or {})
        if isinstance(body, bytes):
            self.body = body
        else:
            self.body = json.dumps(body).encode()
            self.headers.setdefault("Content-Type", "application/json")


class StandIn:
    """Subclass and implement handle(request) -> Response. Records every request (method, path, headers, size)."""

    def __init__(
        self,
        ca: TestCA,
        hostnames: tuple[str, ...],
        *,
        require_client_cert: bool = False,
        max_body: int = 64 * 1024 * 1024,
    ) -> None:
        self.ca = ca
        self.hostnames = hostnames
        self.require_client_cert = require_client_cert
        self.max_body = max_body
        self.requests: list[Request] = []
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def handle(self, req: Request) -> Response:  # pragma: no cover - abstract
        raise NotImplementedError

    def _ssl(self) -> ssl.SSLContext:
        issued = self.ca.server(*self.hostnames)
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "c.pem").write_bytes(issued.cert_pem)
            (Path(d) / "k.pem").write_bytes(issued.key_pem)
            ctx.load_cert_chain(str(Path(d) / "c.pem"), str(Path(d) / "k.pem"))
        if self.require_client_cert:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cadata=self.ca.cert_pem.decode())
        return ctx

    def start(self) -> StandIn:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # keep test output quiet
                return

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length > outer.max_body:
                    self.send_response(413)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = self.rfile.read(length) if length else b""
                req = Request(self, body)
                with outer._lock:
                    outer.requests.append(req)
                res = outer.handle(req)
                self.send_response(res.status)
                for k, v in res.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(res.body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(res.body)

            do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = do_PATCH = _serve  # noqa: N815 - http.server's naming

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                # a client that aborts (failed TLS handshake, refused client certificate, cut-off upload) is an
                # expected test scenario: record it instead of printing a traceback
                import sys

                outer.errors.append(repr(sys.exc_info()[1]))

        self._server = Server(("127.0.0.1", 0), Handler)
        self._server.socket = self._ssl().wrap_socket(self._server.socket, server_side=True)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def __enter__(self) -> StandIn:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
