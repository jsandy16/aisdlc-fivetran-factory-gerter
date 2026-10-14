"""Secrets from the DataOps platform's Vault (the dataops layout, ADR-0026).

The platform keeps one Vault token per project in Secrets Manager (`/{service_env}/pcp/{entity}/vault-token`); secrets
live in KV v2 under the project (`v1/secret/data/{entity_path}/base/{name}`). The project entity comes from the
platform settings or, as the DataOps containers do, from segments 2-5 of AWS_ROLE_ARN (`arn-rd-dpl-studydata-dev-…`
→ `rd-dpl-studydata-dev`). Token and values never leave memory, are never logged and stay in SecretValue.

Failure classes: Vault unreachable or 5xx → transient; token missing or refused → config; missing secret or key →
config.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from factory_core.errors import ConfigError, ErrorClass, SecurityError, TransientError
from factory_core.retry import classify

from ..aws import AwsClients
from ..tls import read_capped, refuse_redirect
from .provider import ResolvedRef
from .value import SecretValue

ROLE_ENV = "AWS_ROLE_ARN"


def project_entity(configured: str | None, environ: Mapping[str, str]) -> str:
    if configured:
        return configured
    parts = environ.get(ROLE_ENV, "").strip().split("-")
    if len(parts) < 5 or not all(parts[1:5]):
        raise ConfigError("vault_entity_unknown", f"no project entity: set it in the platform settings or {ROLE_ENV}")
    return "-".join(parts[1:5])


class DataopsVaultProvider:
    def __init__(
        self,
        client: httpx.Client,
        aws: AwsClients,
        *,
        address: str,
        entity: Callable[[], str],
        service_env: str,
        token_secret: str,
        token_region: str,
        kv_path: str,
    ) -> None:
        self.client = client
        self.aws = aws
        self.address = address.rstrip("/")
        self.entity = entity
        self.service_env = service_env
        self.token_secret = token_secret
        self.token_region = token_region
        self.kv_path = kv_path
        self._token: SecretValue | None = None

    def _secretsmanager(self) -> Any:
        if self.token_region == self.aws.region:
            return self.aws.client("secretsmanager")
        kw = {"endpoint_url": self.aws.endpoint_url} if self.aws.endpoint_url else {}
        return self.aws.session.client("secretsmanager", region_name=self.token_region, **kw)

    def _get(self, url: str, token: SecretValue) -> tuple[int, bytes]:
        try:
            with token.reveal() as t, self.client.stream("GET", url, headers={"X-Vault-Token": t}) as res:
                refuse_redirect(res, "Vault")
                return res.status_code, read_capped(res, 1024 * 1024)
        except httpx.TransportError as exc:
            if classify(exc) is ErrorClass.SECURITY:
                raise SecurityError("vault_tls_untrusted", "Vault's certificate is not trusted") from exc
            raise TransientError("vault_unavailable", f"Vault is unreachable ({type(exc).__name__})") from exc

    def _get_token(self, entity: str, refresh: bool) -> SecretValue:
        if self._token is not None and not refresh:
            return self._token
        sid = self.token_secret.format(service_env=self.service_env, entity=entity)
        try:
            res = self._secretsmanager().get_secret_value(SecretId=sid)
        except Exception as exc:
            code = self.aws.error_code(exc)
            if code in ("ResourceNotFoundException", "AccessDeniedException", "AccessDenied"):
                raise ConfigError("vault_token_unavailable", "the project's Vault token cannot be read") from None
            raise
        text = res.get("SecretString")
        if not text:
            raise ConfigError("vault_token_unavailable", "the project's Vault token is empty")
        self._token = SecretValue(text.strip(), "vault-token")
        return self._token

    def _read(self, entity: str, path: str, refresh: bool) -> dict[str, Any]:
        url = f"{self.address}/v1/" + self.kv_path.format(entity_path=entity.replace("-", "/"), path=path)
        for attempt in (0, 1):
            status, body = self._get(url, self._get_token(entity, refresh or attempt == 1))
            if status == 403 and attempt == 0:
                continue  # the token may have been rotated: read it again once
            if status == 404:
                raise ConfigError("secret_missing", f"no secret at {path} in Vault")
            if status in (401, 403):
                raise ConfigError("vault_permission_denied", "Vault refused the project token")
            if status == 429 or status >= 500:
                raise TransientError("vault_unavailable", f"Vault answered {status}")
            if status != 200:
                raise ConfigError("vault_unexpected", f"Vault answered {status}")
            try:
                data = json.loads(body)["data"]["data"]
            except (ValueError, KeyError, TypeError):
                raise ConfigError("secret_format_invalid", f"the secret at {path} is not a KV v2 document") from None
            if not isinstance(data, dict):
                raise ConfigError("secret_format_invalid", f"the secret at {path} is not a KV v2 document")
            return data
        raise ConfigError("vault_permission_denied", "Vault refused the project token")  # pragma: no cover

    def get(self, ref: ResolvedRef, *, purpose: str, rule_id: str | None = None, refresh: bool = False) -> SecretValue:
        values = self._read(self.entity(), ref.path, refresh)
        SecretValue(json.dumps(values, sort_keys=True), ref.name)  # register the whole document with the redactor
        for v in values.values():
            if isinstance(v, str) and v:
                SecretValue(v, ref.name)
        if ref.key is None:
            if len(values) != 1:
                raise ConfigError("secret_key_missing", f"secret '{ref.name}' has several fields: name one with key")
            return SecretValue(str(next(iter(values.values()))), ref.name)
        if ref.key not in values:
            raise ConfigError("secret_key_missing", f"secret '{ref.name}' has no key '{ref.key}'")
        return SecretValue(str(values[ref.key]), ref.name)
