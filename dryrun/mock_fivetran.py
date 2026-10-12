"""A small stand-in for the Fivetran REST API (dry runs only): the endpoints `fivetran deploy` (Connector SDK CLI)
and `pf` call, with the response shapes they read. Every request is appended to <state>/fivetran-requests.jsonl
so the dry run can assert what would have been sent. Not a full emulator.

    python dryrun/mock_fivetran.py <state_dir> <port> <group_name>[,<group_name>...]
"""
from __future__ import annotations

import base64
import json
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

KEY = "pf-dry-key:pf-dry-secret"


class State:
    def __init__(self, root: Path, groups: list[str]):
        self.root = root
        self.lock = threading.Lock()
        self.groups = {f"grp_{g}": {"id": f"grp_{g}", "name": g} for g in groups}
        self.destinations: dict[str, dict] = {}
        self.connections: dict[str, dict] = {}
        self.packages: dict[str, int] = {}


def make_handler(st: State):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, data: dict | None = None, message: str = "") -> None:
            body = json.dumps({"code": "Success" if code < 300 else "Error", "message": message, "data": data or {}}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _record(self, body: bytes) -> None:
            entry = {"method": self.command, "path": self.path, "accept": self.headers.get("Accept"),
                     "content_type": (self.headers.get("Content-Type") or "").split(";")[0]}
            if entry["content_type"] == "application/json" and body:
                entry["json"] = json.loads(body)
            elif body:
                entry["bytes"] = len(body)
            with st.lock, open(st.root / "fivetran-requests.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")

        def _auth(self) -> bool:
            h = self.headers.get("Authorization", "")
            ok = h in (f"Basic {base64.b64encode(KEY.encode()).decode()}",)
            if not ok:
                self._send(401, message="Unauthorized")
            return ok

        def _route(self, method: str) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            self._record(body)
            if not self._auth():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path.rstrip("/")
            with st.lock:
                if method == "GET" and p == "/v1/groups":
                    return self._send(200, {"items": list(st.groups.values()), "next_cursor": None})
                m = re.fullmatch(r"/v1/groups/([^/]+)/(connections|connectors)", p)
                if method == "GET" and m:
                    items = [c for c in st.connections.values() if c["group_id"] == m.group(1)
                             and (not q.get("schema") or c["schema"] == q["schema"])]
                    return self._send(200, {"items": items, "next_cursor": None})
                if method == "POST" and p == "/v1/connector-sdk/packages":
                    pid = f"pkg_{uuid.uuid4().hex[:10]}"
                    st.packages[pid] = len(body)
                    (st.root / f"{pid}.upload").write_bytes(body)
                    return self._send(201, {"id": pid})
                m = re.fullmatch(r"/v1/connector-sdk/packages/([^/]+)", p)
                if method == "DELETE" and m:
                    st.packages.pop(m.group(1), None)
                    return self._send(200)
                if method == "POST" and p in ("/v1/connectors", "/v1/connections"):
                    req = json.loads(body)
                    cid = f"con_{uuid.uuid4().hex[:10]}"
                    c = {"id": cid, "service": req.get("service", "connector_sdk"), "group_id": req["group_id"],
                         "schema": req.get("config", {}).get("schema") or req.get("schema") or req.get("connection_name") or req.get("name"),
                         "paused": True, "sync_frequency": 360, "schedule_type": "auto", "config": req.get("config", {})}
                    st.connections[cid] = c
                    return self._send(201, c)
                m = re.fullmatch(r"/v1/(connectors|connections)/([^/]+)", p)
                if m and m.group(2) in st.connections:
                    c = st.connections[m.group(2)]
                    if method == "GET":
                        return self._send(200, c)
                    if method == "PATCH":
                        req = json.loads(body or b"{}")
                        for k in ("paused", "sync_frequency", "schedule_type", "daily_sync_time"):
                            if k in req:
                                c[k] = req[k]
                        if "config" in req:
                            c["config"] = req["config"]
                        return self._send(200, c)
                if method == "POST" and p == "/v1/destinations":
                    req = json.loads(body)
                    st.destinations[req["group_id"]] = {**req, "id": req["group_id"], "setup_status": "connected"}
                    return self._send(201, st.destinations[req["group_id"]])
                m = re.fullmatch(r"/v1/destinations/([^/]+)", p)
                if method == "GET" and m:
                    if m.group(1) not in st.destinations:
                        return self._send(404, message="Destination not found")
                    return self._send(200, st.destinations[m.group(1)])
            return self._send(404, message=f"mock: no route for {method} {p}")

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PATCH(self):
            self._route("PATCH")

        def do_DELETE(self):
            self._route("DELETE")

    return H


def serve(root: Path, port: int, groups: list[str]) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(State(root, groups)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    serve(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3].split(","))
    threading.Event().wait()
