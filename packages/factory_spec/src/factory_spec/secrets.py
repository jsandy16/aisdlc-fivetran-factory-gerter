"""Where each secret is read from (ADR-0008, amended): the environment decides, not the logic.

Deployed environments: a per-secret override in `environments.yaml` → `secrets.overrides`, else the environment's
`secrets.default`, else the provider declared with the secret (kept for older specs). Local runs: every secret comes
from the developer's local file (`FACTORY_LOCAL_SECRETS_FILE`), which deployed configuration never sets and the
runtime refuses in ppt and prod; platform settings say which environments may be run locally (`allow_local_secrets`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

LOCAL_SECRETS_ENV = "FACTORY_LOCAL_SECRETS_FILE"


def effective_provider(spec: Any, env: str, name: str) -> str | None:
    binding = spec.environments.environments[env].secrets
    declared = spec.integrations.secrets[name].provider
    return binding.overrides.get(name) or binding.default or declared  # type: ignore[no-any-return]


def local_secrets_path(team: str, env: str, environ: Mapping[str, str] | None = None) -> Path:
    """The developer's secrets file: outside the repository, never committed, never read by agents."""
    e = os.environ if environ is None else environ
    if e.get(LOCAL_SECRETS_ENV):
        return Path(e[LOCAL_SECRETS_ENV]).expanduser()
    return Path.home() / ".factory" / "secrets" / team / f"{env}.yaml"
