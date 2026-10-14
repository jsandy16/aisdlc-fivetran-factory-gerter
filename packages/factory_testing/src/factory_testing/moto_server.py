"""moto as a local server (S3, SQS, DynamoDB, Secrets Manager, STS), threaded in-process.

End-to-end tests point the runtime at it through Overrides.aws_endpoint_url (never through AWS_ENDPOINT_URL, which
ppt/prod refuse). For memory measurements run moto in a separate process instead, so its memory is not counted.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from moto.server import ThreadedMotoServer

SYNTHETIC_CREDENTIALS = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing"}


@contextmanager
def moto_server() -> Iterator[str]:
    """Start moto on a free local port; yields its endpoint URL."""
    for k, v in SYNTHETIC_CREDENTIALS.items():
        os.environ.setdefault(k, v)
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.stop()


@contextmanager
def moto_subprocess() -> Iterator[str]:
    """moto in its own process: separate state from in-process servers (e.g. a stand-in for another account)."""
    import socket
    import subprocess
    import sys
    import time
    import urllib.request

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(  # noqa: S603 - fixed argv: this interpreter running moto's own server module
        [sys.executable, "-m", "moto.server", "-H", "127.0.0.1", "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/moto-api/", timeout=1).close()  # noqa: S310 - local stand-in
                break
            except OSError:
                time.sleep(0.1)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)
