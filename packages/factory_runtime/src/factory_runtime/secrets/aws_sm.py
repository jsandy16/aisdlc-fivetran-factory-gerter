"""AWS Secrets Manager provider (team account; through the VPC endpoint in AWS)."""

from __future__ import annotations

import json
import re
from typing import Any

from factory_core.errors import ConfigError

from ..aws import AwsClients
from .provider import ResolvedRef
from .value import SecretValue

_VERSION_ID = re.compile(r"^[A-Za-z0-9-]{32,64}$")


class AwsSecretsManagerProvider:
    def __init__(self, aws: AwsClients, prefix: str = "") -> None:
        self.aws = aws
        self.prefix = prefix

    def get(self, ref: ResolvedRef, *, purpose: str, rule_id: str | None = None, refresh: bool = False) -> SecretValue:
        kw: dict[str, Any] = {"SecretId": self.prefix + ref.path}
        if ref.version:
            kw["VersionId" if _VERSION_ID.match(ref.version) else "VersionStage"] = ref.version
        try:
            res = self.aws.client("secretsmanager").get_secret_value(**kw)
        except Exception as exc:
            code = self.aws.error_code(exc)
            if code == "ResourceNotFoundException":
                raise ConfigError("secret_missing", f"secret '{ref.name}' does not exist at {ref.path}") from None
            if code in ("AccessDeniedException", "AccessDenied"):
                raise ConfigError("secret_access_denied", f"no permission to read secret '{ref.name}'") from None
            raise
        text = res.get("SecretString")
        if text is None:
            raise ConfigError("secret_format_invalid", f"secret '{ref.name}' is binary; text is expected")
        if ref.key is None:
            return SecretValue(text, ref.name)
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            raise ConfigError("secret_format_invalid", f"secret '{ref.name}' is not a JSON object") from None
        if not isinstance(doc, dict) or ref.key not in doc:
            raise ConfigError("secret_key_missing", f"secret '{ref.name}' has no key '{ref.key}'")
        SecretValue(text, ref.name)  # register the whole document with the redactor too
        return SecretValue(str(doc[ref.key]), ref.name)
