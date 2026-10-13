"""One TLS and HTTP policy for every outbound call (portal, APIs, HTTPS delivery, Vault, Fivetran).

- certificates are always verified: certifi's CA bundle, or a CA pinned for the destination; TLS 1.2 or newer;
- the process environment is ignored (trust_env=False): SSL_CERT_FILE, REQUESTS_CA_BUNDLE and *_PROXY cannot change
  what is trusted or where traffic goes;
- redirects are never followed (a 3xx is a configuration error at the caller);
- client certificates (mTLS) are written to 0600 files created exclusively in a private folder and deleted as soon as
  the SSL context has loaded them;
- response bodies are read with a size cap.
A certificate verification failure surfaces as a `security` error (factory_core.retry.classify), never transient.
"""

from __future__ import annotations

import os
import ssl
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import certifi
import httpx
from factory_core.errors import ConfigError

from .secrets.value import SecretValue

DEFAULT_MAX_BODY = 16 * 1024 * 1024


@contextmanager
def _private_file(folder: Path, name: str, data: str) -> Iterator[Path]:
    path = folder / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
        yield path
    finally:
        path.unlink(missing_ok=True)


def ssl_context(
    *,
    pinned_ca: str | None = None,
    extra_ca: bytes | None = None,
    client_cert: tuple[SecretValue, SecretValue] | None = None,
) -> ssl.SSLContext:
    ctx = (
        ssl.create_default_context(cadata=pinned_ca)
        if pinned_ca
        else ssl.create_default_context(cafile=certifi.where())
    )
    if extra_ca:  # tests only (Overrides.extra_ca_pem): trust the stand-ins' CA as well
        ctx.load_verify_locations(cadata=extra_ca.decode())
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if client_cert is not None:
        cert, key = client_cert
        with tempfile.TemporaryDirectory(prefix="factory-mtls-") as d:
            os.chmod(d, 0o700)
            with (
                cert.reveal() as c,
                key.reveal() as k,
                _private_file(Path(d), "cert.pem", c) as cp,
                _private_file(Path(d), "key.pem", k) as kp,
            ):
                ctx.load_cert_chain(certfile=str(cp), keyfile=str(kp))
    return ctx


def http_client(
    ctx: ssl.SSLContext,
    *,
    connect_timeout: float = 10.0,
    read_timeout: float = 60.0,
    base_url: str = "",
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
    return httpx.Client(
        base_url=base_url,
        verify=ctx,
        trust_env=False,
        follow_redirects=False,
        timeout=timeout,
        transport=transport,
        headers={"User-Agent": "pipeline-factory-runtime"},
    )


def refuse_redirect(response: httpx.Response, what: str) -> None:
    if 300 <= response.status_code < 400:
        raise ConfigError("redirect_refused", f"{what} answered with a redirect ({response.status_code}); not followed")


def read_capped(response: httpx.Response, max_bytes: int = DEFAULT_MAX_BODY) -> bytes:
    """Read a streamed response body, refusing anything larger than max_bytes."""
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise ConfigError("response_too_large", f"the response is larger than {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
