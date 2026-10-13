"""The provider stack a task uses: Audited(Cached(Router(Vault | Secrets Manager))), built from the env binding."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from factory_core.errors import ConfigError, FactoryError

from ..services import Services
from ..tls import http_client, ssl_context
from .aws_sm import AwsSecretsManagerProvider
from .cache import CachedProvider
from .provider import ResolvedRef, SecretProvider, resolve_ref
from .value import SecretValue
from .vault import VaultClient, VaultProvider


class Router:
    def __init__(self, providers: dict[str, Callable[[], SecretProvider]]) -> None:
        self._factories = providers
        self._built: dict[str, SecretProvider] = {}

    def get(self, ref: ResolvedRef, *, purpose: str, rule_id: str | None = None, refresh: bool = False) -> SecretValue:
        factory = self._factories.get(ref.provider)
        if factory is None:
            raise ConfigError("secret_provider_unbound", f"no {ref.provider} binding for secret '{ref.name}'")
        if ref.provider not in self._built:
            self._built[ref.provider] = factory()
        return self._built[ref.provider].get(ref, purpose=purpose, rule_id=rule_id, refresh=refresh)


class AuditedProvider:
    """One audit event and one metric per `get`: who asked, for what, the outcome; never the value or a hash of it."""

    def __init__(self, inner: SecretProvider, on_event: Callable[..., None], services: Services) -> None:
        self.inner = inner
        self.on_event = on_event
        self.services = services

    def get(self, ref: ResolvedRef, *, purpose: str, rule_id: str | None = None, refresh: bool = False) -> SecretValue:
        t0 = time.monotonic()
        outcome, code = "ok", None
        try:
            return self.inner.get(ref, purpose=purpose, rule_id=rule_id, refresh=refresh)
        except FactoryError as exc:
            outcome, code = "failed", exc.code
            raise
        except Exception:
            outcome, code = "failed", "unexpected_error"
            raise
        finally:
            ms = int((time.monotonic() - t0) * 1000)
            self.on_event(
                "secret_access",
                secret=ref.name,
                provider=ref.provider,
                path=ref.path,
                key=ref.key,
                version=ref.version,
                purpose=purpose,
                rule_id=rule_id,
                outcome=outcome,
                code=code,
                latency_ms=ms,
            )
            self.services.log.metric("SecretAccess", 1, "Count", provider=ref.provider, outcome=outcome)


class Secrets:
    """What ops use: `secrets.get("portal_client", purpose="portal auth")` by the spec's secret name."""

    def __init__(self, spec: Any, tokens: dict[str, str], provider: SecretProvider) -> None:
        self.spec = spec
        self.tokens = tokens
        self.provider = provider

    def ref(self, name: str) -> ResolvedRef:
        declared = self.spec.integrations.secrets.get(name)
        if declared is None:
            raise ConfigError("secret_undeclared", f"secret '{name}' is not declared in integrations.yaml")
        return resolve_ref(name, declared, self.tokens)

    def get(self, name: str, *, purpose: str, rule_id: str | None = None, refresh: bool = False) -> SecretValue:
        return self.provider.get(self.ref(name), purpose=purpose, rule_id=rule_id, refresh=refresh)


def build_secrets(services: Services, spec: Any, env: str, on_event: Callable[..., None]) -> Secrets:
    cfg = services.config
    binding = spec.environments.environments[env].secrets
    factories: dict[str, Callable[[], SecretProvider]] = {}
    if binding.aws_secrets_manager is not None:
        prefix = binding.aws_secrets_manager.prefix
        factories["aws_secrets_manager"] = lambda: AwsSecretsManagerProvider(services.aws, prefix)
    if binding.vault is not None:
        vb = binding.vault

        def vault() -> SecretProvider:
            if not cfg.vault_address or not cfg.vault_role:
                raise ConfigError("vault_not_configured", "the runtime has no Vault address or role")
            ctx = ssl_context(extra_ca=services.extra_ca_pem)
            client = VaultClient(
                http_client(ctx, connect_timeout=5, read_timeout=15),
                cfg.vault_address,
                role=cfg.vault_role,
                auth_mount=cfg.vault_auth_mount,
                namespace=vb.namespace,
                iam_server_id=cfg.vault_iam_server_id,
                region=cfg.region,
                credentials=lambda: services.aws.session.get_credentials(),
                clock=services.clock,
            )
            return VaultProvider(client, vb.mount)

        factories["vault"] = vault
    tokens = {"env": env, "team": cfg.team, "pipeline_id": cfg.pipeline_id}
    stack = AuditedProvider(CachedProvider(Router(factories)), on_event, services)
    return Secrets(spec, tokens, stack)
