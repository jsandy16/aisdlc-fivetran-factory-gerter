"""TLS helpers shared by sources: always-verifying contexts, with an optional pinned CA bundle from a secret."""
from __future__ import annotations

import os
import ssl
import tempfile

from ..errors import ConfigError


def ca_pem(source: dict, configuration: dict) -> str | None:
    ref = source.get("ca_cert")
    if not ref:
        return None
    pem = configuration.get(ref["secret"])
    if not pem or "BEGIN CERTIFICATE" not in pem:
        raise ConfigError(f"configuration key '{ref['secret']}' must hold a PEM certificate bundle")
    return pem


def verifying_context(source: dict, configuration: dict) -> ssl.SSLContext:
    """Certificate and host name are verified (system trust store, or only the pinned CA bundle if given)."""
    pem = ca_pem(source, configuration)
    try:
        return ssl.create_default_context(cadata=pem) if pem else ssl.create_default_context()
    except ssl.SSLError as exc:
        raise ConfigError(f"CA bundle could not be loaded ({type(exc).__name__})") from None


def ca_file(source: dict, configuration: dict) -> str | None:
    """libpq needs a file path: the bundle is written to a private temp file (0600) for this process."""
    pem = ca_pem(source, configuration)
    if pem is None:
        return None
    fd, path = tempfile.mkstemp(prefix="pf-ca-", suffix=".pem")
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(pem)
    return path
