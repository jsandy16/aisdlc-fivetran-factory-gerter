"""A small Vault client on httpx (decision P3-1; replaces hvac): AWS IAM login, KV v2 reads, token renewal.

Login signs an STS GetCallerIdentity request with the workload's own credentials (botocore SigV4, regional STS) and
sends it to Vault, which forwards it to STS to learn the caller's role ARN; the Vault role is bound to that ARN. The
X-Vault-AWS-IAM-Server-ID header is signed too, so a captured login cannot be replayed against another Vault.
The token lives in memory only and is renewed (or re-obtained) before it expires.

Failure classes: Vault sealed or unreachable -> transient; permission denied -> one re-login, then config; missing
secret or deleted version -> config.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from factory_core.errors import ConfigError, ErrorClass, SecurityError, TransientError
from factory_core.retry import classify

from ..tls import read_capped, refuse_redirect
from .provider import ResolvedRef
from .value import SecretValue

_STS_BODY = "Action=GetCallerIdentity&Version=2011-06-15"


@dataclass(frozen=True)
class _Res:
    status: int
    body: bytes


class VaultClient:
    def __init__(
        self,
        client: httpx.Client,
        address: str,
        *,
        role: str,
        auth_mount: str = "aws",
        namespace: str | None = None,
        iam_server_id: str | None = None,
        region: str = "eu-west-1",
        credentials: Callable[[], Any],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.address = address.rstrip("/")
        self.role = role
        self.auth_mount = auth_mount
        self.namespace = namespace
        self.iam_server_id = iam_server_id
        self.region = region
        self.credentials = credentials
        self.clock = clock
        self._token: SecretValue | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ transport

    def _headers(self, token: bool = True) -> dict[str, str]:
        h: dict[str, str] = {}
        if self.namespace:
            h["X-Vault-Namespace"] = self.namespace
        if token and self._token is not None:
            with self._token.reveal() as t:
                h["X-Vault-Token"] = t
        return h

    def _send(self, method: str, path: str, *, token: bool = True, body: dict[str, Any] | None = None) -> _Res:
        try:
            with self.client.stream(
                method, f"{self.address}/v1/{path}", headers=self._headers(token), json=body
            ) as res:
                refuse_redirect(res, "Vault")
                return _Res(res.status_code, read_capped(res, 1024 * 1024))
        except httpx.TransportError as exc:
            if classify(exc) is ErrorClass.SECURITY:
                raise SecurityError("vault_tls_untrusted", "Vault's certificate is not trusted") from exc
            raise TransientError("vault_unavailable", f"Vault is unreachable ({type(exc).__name__})") from exc

    @staticmethod
    def _json(res: _Res) -> dict[str, Any]:
        try:
            doc = json.loads(res.body or b"{}")
        except json.JSONDecodeError:
            raise ConfigError("vault_response_invalid", "Vault returned something that is not JSON") from None
        return doc if isinstance(doc, dict) else {}

    # ------------------------------------------------------------------ auth

    def _signed_login_payload(self) -> dict[str, Any]:
        url = f"https://sts.{self.region}.amazonaws.com/"
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        if self.iam_server_id:
            headers["X-Vault-AWS-IAM-Server-ID"] = self.iam_server_id
        req = AWSRequest(method="POST", url=url, data=_STS_BODY, headers=headers)
        SigV4Auth(self.credentials(), "sts", self.region).add_auth(req)
        hdrs = {k: [v] for k, v in req.headers.items()}
        return {
            "role": self.role,
            "iam_http_request_method": "POST",
            "iam_request_url": base64.b64encode(url.encode()).decode(),
            "iam_request_body": base64.b64encode(_STS_BODY.encode()).decode(),
            "iam_request_headers": base64.b64encode(json.dumps(hdrs).encode()).decode(),
        }

    def login(self) -> None:
        res = self._send("POST", f"auth/{self.auth_mount}/login", token=False, body=self._signed_login_payload())
        self._raise_for_status(res, "login")
        auth = self._json(res).get("auth") or {}
        token = auth.get("client_token")
        if not isinstance(token, str) or not token:
            raise ConfigError("vault_login_failed", "Vault returned no token")
        self._token = SecretValue(token, "vault_token")
        self._expires_at = self.clock() + float(auth.get("lease_duration") or 0)

    def _ensure_token(self) -> None:
        with self._lock:
            if self._token is None or self.clock() > self._expires_at - 60:
                self.login()

    def _raise_for_status(self, res: _Res, what: str) -> None:
        if res.status < 400:
            return
        if res.status in (429, 500, 502, 503, 504):
            raise TransientError("vault_unavailable", f"Vault {what} failed ({res.status})")
        if res.status == 403:
            raise ConfigError("vault_access_denied", f"Vault refused the {what} (403)")
        if res.status == 404:
            raise ConfigError("secret_missing", f"Vault has no such secret ({what})")
        raise ConfigError("vault_request_failed", f"Vault {what} failed ({res.status})")

    # ------------------------------------------------------------------ KV v2

    def read_kv(self, mount: str, path: str, version: int | None = None) -> dict[str, Any]:
        q = f"?version={version}" if version is not None else ""
        for attempt in (1, 2):
            self._ensure_token()
            res = self._send("GET", f"{mount}/data/{path}{q}")
            if res.status == 403 and attempt == 1:  # token revoked or expired early: log in again once
                with self._lock:
                    self._token = None
                continue
            self._raise_for_status(res, "read")
            data = self._json(res).get("data") or {}
            meta = data.get("metadata") or {}
            if meta.get("destroyed") or meta.get("deletion_time"):
                raise ConfigError("secret_version_unavailable", "the requested secret version is deleted or destroyed")
            values = data.get("data")
            if not isinstance(values, dict):
                raise ConfigError("secret_missing", "Vault returned no secret data")
            return values
        raise ConfigError("vault_access_denied", "Vault refused the read after a fresh login")  # pragma: no cover

    def renew(self) -> None:
        self._ensure_token()
        res = self._send("POST", "auth/token/renew-self", body={})
        self._raise_for_status(res, "renewal")
        auth = self._json(res).get("auth") or {}
        self._expires_at = self.clock() + float(auth.get("lease_duration") or 0)

    def health(self) -> int:
        return self._send("GET", "sys/health", token=False).status


class VaultProvider:
    def __init__(self, client: VaultClient, mount: str) -> None:
        self.client = client
        self.mount = mount

    def get(self, ref: ResolvedRef, *, purpose: str, rule_id: str | None = None, refresh: bool = False) -> SecretValue:
        version = int(ref.version) if ref.version and ref.version.isdigit() else None
        values = self.client.read_kv(self.mount, ref.path, version)
        if ref.key is None:
            if len(values) != 1:
                raise ConfigError("secret_key_missing", f"secret '{ref.name}' has several fields; name the key")
            return SecretValue(str(next(iter(values.values()))), ref.name)
        if ref.key not in values:
            raise ConfigError("secret_key_missing", f"secret '{ref.name}' has no key '{ref.key}'")
        for v in values.values():
            if isinstance(v, str):
                SecretValue(v, ref.name)  # register every field with the redactor
        return SecretValue(str(values[ref.key]), ref.name)
