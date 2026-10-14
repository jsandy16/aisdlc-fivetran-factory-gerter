"""Refuse to run in ppt/prod when the environment could silently redirect traffic or trust (ADR-0016).

These variables change where AWS SDKs connect, which CAs are trusted or which proxy sees the traffic. They have no
place in a production workload, so their presence is treated as a security failure (exit 77), not a warning.
"""

from __future__ import annotations

from collections.abc import Mapping

from factory_core.errors import SecurityError

HAZARDOUS = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "AWS_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "FACTORY_LOCAL_SECRETS_FILE",  # local runs only: secrets from a developer's file
)
HAZARDOUS_PREFIXES = ("AWS_ENDPOINT_URL",)
STRICT_ENVS = frozenset({"ppt", "prod", "prd"})


def hazardous_settings(environ: Mapping[str, str]) -> list[str]:
    found = [k for k in HAZARDOUS if environ.get(k)]
    found += [k for k in environ if k.startswith(HAZARDOUS_PREFIXES) and environ.get(k)]
    return sorted(set(found))


def check_environment(env: str, environ: Mapping[str, str], allowed: Mapping[str, str] | None = None) -> None:
    """`allowed`: variables the image declares with these exact values (the dataops layout's corporate proxy and
    CA bundle, from factory.toml); the same variable with any other value is still refused."""
    if env not in STRICT_ENVS:
        return
    ok = allowed or {}
    found = [k for k in hazardous_settings(environ) if k not in ok or environ.get(k) != ok[k]]
    if found:
        raise SecurityError(
            "environment_not_allowed",
            f"{', '.join(found)} must not be set in {env} (they redirect connections or change trusted CAs)",
        )
