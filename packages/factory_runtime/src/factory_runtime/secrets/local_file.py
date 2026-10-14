"""Local secrets file for development runs (`factory run`): never deployed, never committed, never read by agents.

Layout (YAML or JSON), keyed by the secret's rendered path, the same path Vault or Secrets Manager would use:

    clinops/dev/portal-client:            # a whole secret (a JSON document when it has several fields)
      client_id: "..."
      client_secret: "..."
    clinops/dev/cro-upload: {token: "..."}

The file must be private to its owner (no group or other permissions); a wider file is refused as a security error.
Every value read is wrapped in SecretValue, so redaction and audit events are the same as with the real stores.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from factory_core.errors import ConfigError, SecurityError

from .provider import ResolvedRef
from .value import SecretValue


class LocalFileProvider:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._doc: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._doc is not None:
            return self._doc
        if not self.path.is_file():
            hint = "create it with `factory secrets template`"
            raise ConfigError("local_secrets_file_missing", f"no local secrets file at {self.path}; {hint}")
        if self.path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SecurityError("local_secrets_file_permissions",
                                f"{self.path} is readable by others; run: chmod 600 {self.path}")  # fmt: skip
        from factory_core import yamlio

        try:
            doc = yamlio.to_plain(yamlio.load_path(self.path))
        except Exception:
            raise ConfigError("local_secrets_file_invalid", f"{self.path} is not valid YAML or JSON") from None
        if not isinstance(doc, dict):
            raise ConfigError("local_secrets_file_invalid", f"{self.path} must map secret paths to values")
        self._doc = doc
        return doc

    def get(self, ref: ResolvedRef, *, purpose: str, rule_id: str | None = None, refresh: bool = False) -> SecretValue:
        if refresh:
            self._doc = None
        entry = self._load().get(ref.path)
        if entry is None or entry == "" or entry == {}:
            raise ConfigError("secret_missing", f"secret '{ref.name}' has no value at '{ref.path}' in the local file")
        if isinstance(entry, dict):
            whole = json.dumps(entry, sort_keys=True)
            SecretValue(whole, ref.name)  # register the whole document with the redactor too
            if ref.key is None:
                return SecretValue(whole, ref.name)
            value = entry.get(ref.key)
            if value is None or value == "":
                raise ConfigError("secret_key_missing", f"secret '{ref.name}' has no key '{ref.key}' in the local file")
            return SecretValue(str(value), ref.name)
        if ref.key is not None:
            raise ConfigError(
                "secret_key_missing", f"secret '{ref.name}' needs key '{ref.key}'; the local value is text"
            )
        return SecretValue(str(entry), ref.name)
