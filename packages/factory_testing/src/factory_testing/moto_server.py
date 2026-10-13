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
