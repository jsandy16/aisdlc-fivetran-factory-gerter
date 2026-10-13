"""Secrets (ADR-0008): SecretValue, Secrets Manager, the built-in Vault client, cache, audit; the TLS policy."""

from __future__ import annotations

import copy
import json
import pickle
from collections.abc import Iterator
from typing import Any

import boto3
import httpx
import pytest
from botocore.credentials import Credentials
from factory_core.errors import ConfigError, ErrorClass, SecurityError, TransientError
from factory_core.retry import classify
from factory_runtime.aws import AwsClients
from factory_runtime.secrets.aws_sm import AwsSecretsManagerProvider
from factory_runtime.secrets.cache import CachedProvider
from factory_runtime.secrets.provider import ResolvedRef
from factory_runtime.secrets.router import AuditedProvider, Router
from factory_runtime.secrets.value import SecretValue
from factory_runtime.secrets.vault import VaultClient, VaultProvider
from factory_runtime.tls import http_client, refuse_redirect, ssl_context
from factory_testing.certs import TestCA
from factory_testing.fake_vault import FakeVault
from factory_testing.netmap import netmap
from rt_helpers import LogCapture, make_config

from factory_core import jsonlog

VAULT_HOST = "vault.dev.acme.example"
SYNTHETIC_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic-not-a-real-key-line-0001\n-----END OPENSSH PRIVATE KEY-----"
)


# ---------------------------------------------------------------- SecretValue


def test_secret_value_cannot_leak_by_accident() -> None:
    s = SecretValue("synthetic-token-value-1", "cro_upload_token")
    assert "synthetic" not in repr(s)
    assert "synthetic" not in str(s)
    assert "synthetic" not in f"{s}"
    for leak in (lambda: pickle.dumps(s), lambda: copy.copy(s), lambda: copy.deepcopy(s), lambda: json.dumps(s)):
        with pytest.raises(TypeError):
            leak()
    with pytest.raises(AttributeError):
        s._value = "x"  # type: ignore[misc]
    with s.reveal() as v:
        assert v == "synthetic-token-value-1"
    assert jsonlog.redact("got synthetic-token-value-1 and c3ludGhldGljLXRva2VuLXZhbHVlLTE=") == "got *** and ***"


def test_json_fields() -> None:
    s = SecretValue('{"client_id": "synthetic-id-1", "client_secret": "synthetic-secret-1"}', "portal_client")
    f = s.json_fields()
    with f["client_secret"].reveal() as v:
        assert v == "synthetic-secret-1"
    with pytest.raises(ConfigError):
        SecretValue("not json", "x").json_fields()


# ---------------------------------------------------------------- AWS Secrets Manager


def _ref(name: str, provider: str, path: str, key: str | None = None, version: str | None = None) -> ResolvedRef:
    return ResolvedRef(name, provider, path, key, version, "api_key")


def test_secrets_manager(aws: None, workdir: Any) -> None:
    sm = boto3.client("secretsmanager", region_name="eu-west-1")
    sm.create_secret(Name="factory/clinops/dev/cro-upload", SecretString=json.dumps({"token": "synthetic-v1"}))
    sm.put_secret_value(SecretId="factory/clinops/dev/cro-upload", SecretString=json.dumps({"token": "synthetic-v2"}))
    p = AwsSecretsManagerProvider(AwsClients(make_config(workdir, "unit_naming")), "factory/")
    with p.get(_ref("t", "aws_secrets_manager", "clinops/dev/cro-upload", "token"), purpose="test").reveal() as v:
        assert v == "synthetic-v2"
    prev = p.get(_ref("t", "aws_secrets_manager", "clinops/dev/cro-upload", "token", "AWSPREVIOUS"), purpose="test")
    with prev.reveal() as v:
        assert v == "synthetic-v1"
    with pytest.raises(ConfigError) as ei:
        p.get(_ref("t", "aws_secrets_manager", "clinops/dev/missing"), purpose="test")
    assert ei.value.code == "secret_missing"
    with pytest.raises(ConfigError) as ei:
        p.get(_ref("t", "aws_secrets_manager", "clinops/dev/cro-upload", "nokey"), purpose="test")
    assert ei.value.code == "secret_key_missing"


# ---------------------------------------------------------------- Vault


@pytest.fixture
def ca() -> TestCA:
    return TestCA()


@pytest.fixture
def vault(ca: TestCA) -> Iterator[FakeVault]:
    v = FakeVault(
        ca, VAULT_HOST, role="factory-clinops_data-study_pkg_delivery", server_id=VAULT_HOST, namespace="clinops"
    )
    v.put("clinops/dev/partner-sftp", {"private_key": SYNTHETIC_KEY, "user": "factory-delivery"})
    v.start()
    with netmap({(VAULT_HOST, 8200): ("127.0.0.1", v.port)}):
        yield v
    v.stop()


def _client(ca: TestCA | None, **kw: Any) -> VaultClient:
    ctx = ssl_context(extra_ca=ca.cert_pem if ca else None)
    args: dict[str, Any] = {
        "role": "factory-clinops_data-study_pkg_delivery",
        "namespace": "clinops",
        "iam_server_id": VAULT_HOST,
        "credentials": lambda: Credentials("testing", "testing"),
    }
    args.update(kw)
    return VaultClient(http_client(ctx), f"https://{VAULT_HOST}:8200", **args)


def test_vault_login_and_kv(ca: TestCA, vault: FakeVault) -> None:
    p = VaultProvider(_client(ca), "kv")
    ref = ResolvedRef("partner_sftp_key", "vault", "clinops/dev/partner-sftp", "private_key", None, "ssh_private_key")
    with p.get(ref, purpose="sftp delivery").reveal() as v:
        assert v == SYNTHETIC_KEY
    assert vault.logins == 1
    # the token is reused, never logged
    p.get(ref, purpose="sftp delivery")
    assert vault.logins == 1
    assert "hvs." not in jsonlog.redact(" ".join(vault.tokens))


def test_vault_versions_and_deletion(ca: TestCA, vault: FakeVault) -> None:
    vault.put("clinops/dev/partner-sftp", {"private_key": "synthetic-rotated-key-2", "user": "u"})
    p = VaultProvider(_client(ca), "kv")
    old = ResolvedRef("k", "vault", "clinops/dev/partner-sftp", "private_key", "1", "ssh_private_key")
    with p.get(old, purpose="t").reveal() as v:
        assert v == SYNTHETIC_KEY
    vault.delete_version("clinops/dev/partner-sftp", 1)
    with pytest.raises(ConfigError) as ei:
        p.get(old, purpose="t")
    assert ei.value.code == "secret_version_unavailable"
    with pytest.raises(ConfigError) as ei:
        p.get(ResolvedRef("k", "vault", "clinops/dev/nothing", "x", None, "generic"), purpose="t")
    assert ei.value.code == "secret_missing"


def test_vault_sealed_is_transient_and_forbidden_is_config(ca: TestCA, vault: FakeVault) -> None:
    c = _client(ca)
    vault.mode = "sealed"
    with pytest.raises(TransientError):
        c.read_kv("kv", "clinops/dev/partner-sftp")
    vault.mode = "forbidden"
    with pytest.raises(ConfigError) as ei:
        c.read_kv("kv", "clinops/dev/partner-sftp")
    assert ei.value.code == "vault_access_denied"
    assert vault.logins == 2  # it logged in again once before giving up


def test_vault_token_expiry_relogs_in(ca: TestCA, vault: FakeVault) -> None:
    now = [1000.0]
    c = _client(ca, clock=lambda: now[0])
    c.read_kv("kv", "clinops/dev/partner-sftp")
    now[0] += vault.ttl  # past the lease: the client logs in again before reading
    c.read_kv("kv", "clinops/dev/partner-sftp")
    assert vault.logins == 2


def test_vault_login_needs_signed_server_id(ca: TestCA, vault: FakeVault) -> None:
    with pytest.raises(ConfigError):
        _client(ca, iam_server_id=None).read_kv("kv", "clinops/dev/partner-sftp")


def test_vault_untrusted_certificate_is_security(vault: FakeVault) -> None:
    with pytest.raises(SecurityError) as ei:
        _client(None).read_kv("kv", "clinops/dev/partner-sftp")
    assert ei.value.code == "vault_tls_untrusted"


# ---------------------------------------------------------------- cache and audit


class _Counting:
    def __init__(self) -> None:
        self.calls = 0
        self.value = "synthetic-initial-value"

    def get(self, ref: ResolvedRef, *, purpose: str, rule_id: str | None = None, refresh: bool = False) -> SecretValue:
        self.calls += 1
        return SecretValue(self.value, ref.name)


def test_cache_ttl_and_rotation_mid_run() -> None:
    now = [0.0]
    inner = _Counting()
    cache = CachedProvider(inner, clock=lambda: now[0])
    ref = _ref("t", "aws_secrets_manager", "p")
    cache.get(ref, purpose="x")
    cache.get(ref, purpose="x")
    assert inner.calls == 1
    inner.value = "synthetic-rotated-value"  # rotated while the run is going
    with cache.get(ref, purpose="x").reveal() as v:
        assert v == "synthetic-initial-value"
    with cache.get(ref, purpose="x", refresh=True).reveal() as v:  # after an auth failure the caller refreshes once
        assert v == "synthetic-rotated-value"
    now[0] += 301
    cache.get(ref, purpose="x")
    assert inner.calls == 3


def test_audit_one_event_per_get_without_values(workdir: Any) -> None:
    from factory_runtime.services import Services
    from factory_runtime.state import MemoryRunState

    events: list[dict[str, Any]] = []
    inner = _Counting()
    ref = _ref("t", "aws_secrets_manager", "p")
    with LogCapture() as logs:
        services = Services.build(make_config(workdir, "unit_naming"), state=MemoryRunState("unit_naming"))
        audited = AuditedProvider(CachedProvider(inner), lambda t, **f: events.append({"type": t, **f}), services)
        audited.get(ref, purpose="portal auth", rule_id="r_portal_lookup")
        audited.get(ref, purpose="portal auth", rule_id="r_portal_lookup")
    assert [e["outcome"] for e in events] == ["ok", "ok"]
    assert "synthetic-initial-value" not in json.dumps(events) + logs.text
    assert len(logs.events()) == 2  # SecretAccess metric lines
    with pytest.raises(ConfigError):
        AuditedProvider(Router({}), lambda t, **f: events.append({"type": t, **f}), services).get(ref, purpose="x")
    assert events[-1]["outcome"] == "failed"
    assert events[-1]["code"] == "secret_provider_unbound"


# ---------------------------------------------------------------- TLS policy


def test_tls_ignores_environment_and_refuses_redirects(
    ca: TestCA, vault: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca.pem")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")  # would break every call if honoured
    with http_client(ssl_context(extra_ca=ca.cert_pem)) as c:
        assert c.get(f"https://{VAULT_HOST}:8200/v1/sys/health").status_code == 200
    with pytest.raises(ConfigError):
        refuse_redirect(httpx.Response(302), "the portal")


def test_untrusted_certificate_is_classified_security(vault: FakeVault) -> None:
    with http_client(ssl_context()) as c, pytest.raises(httpx.ConnectError) as ei:
        c.get(f"https://{VAULT_HOST}:8200/v1/sys/health")
    assert classify(ei.value) is ErrorClass.SECURITY
