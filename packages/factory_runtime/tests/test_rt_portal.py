"""Portal client and ops (ADR-0009/0024): faults, auth, paging, validation, guards, snapshot, resolution, TLS."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest
from factory_core.errors import ConfigError, SecurityError, TransientError
from factory_runtime.apiclient import ApiClient
from factory_runtime.config import Overrides
from factory_runtime.secrets.value import SecretValue
from factory_spec.load import load_spec
from factory_spec.sections.integrations import IntegrationsFile
from factory_testing.certs import TestCA
from factory_testing.mock_portal import MockApi, MockPortal, example_from_spec
from factory_testing.netmap import netmap
from rt_helpers import EXAMPLE_REPO, STARTERS, OpHarness, copy_repo, make_config

from factory_core import yamlio
from factory_runtime import apiclient

PORTAL_HOST = "portal.dev.acme.example"
CLIENT = {"client_id": "synthetic-client-id", "client_secret": "synthetic-client-secret-0001"}
MESSAGE = {
    "request_id": "REQ-00000001",
    "study_id": "ONC101",
    "package_id": "PKG0042",
    "bucket": "acme-clinops-landing-dev",
    "prefix": "incoming/ONC101/PKG0042/",
    "file_count": 3,
    "total_bytes": 1200,
}
X_OK = "s3://acme-clinops-delivery-dev/studies/ONC101/in/"
Y_OK = "sftp://sftp-stub.dev.acme.example/inbound/ONC101/"


@pytest.fixture(autouse=True)
def _fresh_breakers() -> Iterator[None]:
    apiclient.reset_process_state()
    yield
    apiclient.reset_process_state()


@pytest.fixture(scope="module")
def portal_spec() -> Any:
    spec = load_spec(EXAMPLE_REPO / "pipelines" / "study_pkg_delivery").spec
    assert spec is not None
    return spec


# ---------------------------------------------------------------- the client with MockTransport


def _secret(name: str, *, purpose: str, refresh: bool = False) -> SecretValue:
    return SecretValue(json.dumps(CLIENT), name)


def _client(spec: Any, handler: Callable[[httpx.Request], httpx.Response], **kw: Any) -> tuple[ApiClient, list[float]]:
    sleeps: list[float] = []
    now = [0.0]

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    c = ApiClient(
        spec.integrations.portal,
        "https://portal.dev.acme.example",
        name="portal",
        secret=kw.pop("secret", _secret),
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
        sleep=sleep,
        **kw,
    )
    return c, sleeps


def _answer(study: str = "ONC101") -> dict[str, Any]:
    return {"study_id": study, "targets": [{"slot": "x", "uri": X_OK, "active": True}]}


def _handler(script: list[httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "synthetic-access-token-1", "expires_in": 3600})
        return script.pop(0) if script else httpx.Response(200, json=_answer())

    return h


def _endpoint(spec: Any) -> Any:
    return spec.integrations.portal.endpoints[0]


def test_retry_after_is_honoured(portal_spec: Any) -> None:
    c, sleeps = _client(portal_spec, _handler([httpx.Response(429, headers={"Retry-After": "7"})]))
    pages = list(c.pages(_endpoint(portal_spec), {"study_id": "ONC101"}))
    assert pages[0].data["study_id"] == "ONC101"
    assert sleeps == [7.0]


def test_503_burst_exhausts_retries_then_opens_the_breaker(portal_spec: Any) -> None:
    c, _ = _client(portal_spec, _handler([httpx.Response(503)] * 20))
    with pytest.raises(TransientError) as ei:
        list(c.pages(_endpoint(portal_spec), {"study_id": "ONC101"}))
    assert ei.value.code == "api_unavailable"
    with pytest.raises(TransientError) as ei:
        list(c.pages(_endpoint(portal_spec), {"study_id": "ONC101"}))
    assert ei.value.code == "circuit_open"


@pytest.mark.parametrize(
    ("response", "error", "code"),
    [
        (httpx.Response(200, json={"study_id": 101, "targets": "x"}), ConfigError, "api_response_invalid"),
        (httpx.Response(302, headers={"Location": "https://evil.example.com/"}), ConfigError, "redirect_refused"),
        (httpx.Response(404, json={}), ConfigError, "api_request_failed"),
        (httpx.Response(403, json={}), ConfigError, "auth_refused"),
        (httpx.Response(200, content=b"x" * (apiclient.MAX_BODY + 1)), ConfigError, "response_too_large"),
    ],
    ids=["schema_drift", "redirect", "not_found", "forbidden", "oversize"],
)
def test_bad_answers(portal_spec: Any, response: httpx.Response, error: type[Exception], code: str) -> None:
    c, _ = _client(portal_spec, _handler([response]))
    with pytest.raises(error) as ei:
        list(c.pages(_endpoint(portal_spec), {"study_id": "ONC101"}))
    assert ei.value.code == code  # type: ignore[attr-defined]
    assert "synthetic" not in str(ei.value)


def test_token_expired_early_is_refreshed_once(portal_spec: Any) -> None:
    tokens: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth2/token":
            tokens.append("t")
            return httpx.Response(200, json={"access_token": f"synthetic-token-{len(tokens)}", "expires_in": 3600})
        if req.headers["authorization"] == "Bearer synthetic-token-1":
            return httpx.Response(401)
        return httpx.Response(200, json=_answer())

    c, _ = _client(portal_spec, h)
    assert next(iter(c.pages(_endpoint(portal_spec), {"study_id": "ONC101"}))).status == 200
    assert len(tokens) == 2


def test_rotated_client_secret_is_refetched_once(portal_spec: Any) -> None:
    fetched: list[bool] = []

    def secret(name: str, *, purpose: str, refresh: bool = False) -> SecretValue:
        fetched.append(refresh)
        cs = "synthetic-new-secret-0002" if refresh else "synthetic-old-secret-0001"
        return SecretValue(json.dumps({"client_id": "synthetic-client-id", "client_secret": cs}), name)

    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth2/token":
            ok = (
                "c3ludGhldGljLW5ldy1zZWNyZXQtMDAwMg" in req.headers["authorization"]
                or "bmV3" in req.headers["authorization"]
            )
            return httpx.Response(200, json={"access_token": "synthetic-at-9"}) if ok else httpx.Response(401)
        return httpx.Response(200, json=_answer())

    c, _ = _client(portal_spec, h, secret=secret)
    list(c.pages(_endpoint(portal_spec), {"study_id": "ONC101"}))
    assert fetched == [False, True]


def test_cursor_paging_and_loop_detection() -> None:
    integ = IntegrationsFile.model_validate(
        yamlio.to_plain(yamlio.load_path(STARTERS / "rest_to_s3" / "spec" / "integrations.yaml"))
    )
    api = integ.apis[0]
    pages = [[{"id": "a"}], [{"id": "b"}], [{"id": "c"}]]

    def h(req: httpx.Request) -> httpx.Response:
        n = int(req.url.params.get("cursor", "0"))
        nxt = str(n + 1) if n + 1 < len(pages) else None
        return httpx.Response(200, json={"items": pages[n], "meta": {"next_cursor": nxt}})

    def key(name: str, *, purpose: str, refresh: bool = False) -> SecretValue:
        return SecretValue("synthetic-api-key-1234", name)

    c = ApiClient(api, "https://api.dev.vendor.example", name="vendor_api", secret=key,
                  transport=httpx.MockTransport(h))  # fmt: skip
    got = [p.data["items"][0]["id"] for p in c.pages(api.endpoints[0], {})]
    assert got == ["a", "b", "c"]

    def loop(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "meta": {"next_cursor": "same"}})

    c = ApiClient(api, "https://api.dev.vendor.example", name="vendor_api", secret=key,
                  transport=httpx.MockTransport(loop))  # fmt: skip
    with pytest.raises(ConfigError) as ei:
        list(c.pages(api.endpoints[0], {}))
    assert ei.value.code == "pagination_loop"


def test_example_from_spec_validates(portal_spec: Any) -> None:
    from factory_spec.dynamic import record_model

    ep = _endpoint(portal_spec)
    record_model("ex", ep.response).model_validate(example_from_spec(ep.response))


# ---------------------------------------------------------------- the ops over TLS against the mock portal


@pytest.fixture
def ca() -> TestCA:
    return TestCA()


@pytest.fixture
def portal(ca: TestCA) -> Iterator[MockPortal]:
    p = MockPortal(ca, PORTAL_HOST, client_id=CLIENT["client_id"], client_secret=CLIENT["client_secret"])
    p.start()
    with netmap({(PORTAL_HOST, 443): ("127.0.0.1", p.port)}):
        yield p
    p.stop()


@pytest.fixture
def harness(tmp_path: Path, workdir: Path, aws: None, ca: TestCA) -> Callable[..., OpHarness]:
    repo = copy_repo(EXAMPLE_REPO, tmp_path / "repo")
    pdir = repo / "pipelines" / "study_pkg_delivery"
    cfg = make_config(workdir, "study_pkg_delivery")
    s3c = boto3.client("s3", region_name="eu-west-1")
    for b in (cfg.staging_bucket, cfg.audit_store):
        s3c.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    boto3.client("secretsmanager", region_name="eu-west-1").create_secret(
        Name="factory/clinops/dev/portal-client", SecretString=json.dumps(CLIENT)
    )

    def make(trust: bool = True, message: dict[str, Any] | None = None) -> OpHarness:
        ov = Overrides(extra_ca_pem=ca.cert_pem if trust else None)
        return OpHarness(pdir, cfg, trigger_body=message or MESSAGE, overrides=ov)

    return make


def test_portal_lookup_and_resolution(portal: MockPortal, harness: Callable[..., OpHarness]) -> None:
    portal.set_targets("ONC101", X_OK, Y_OK)
    h = harness()
    snap = h.run_rule("r_portal_lookup")[0]
    assert snap.destinations == {"x": X_OK, "y": Y_OK}
    dest = h.run_rule("r_resolve_destinations")[0]
    assert {s: d.entry_id for s, d in dest.destinations.items()} == {"x": "dev_delivery_s3", "y": "dev_stub_sftp"}
    types = [e["type"] for e in h.audit.events]
    assert types.count("destination_resolution") == 2
    assert "portal_call" in types
    assert all("synthetic-client-secret" not in json.dumps(e) for e in h.audit.events)
    # the portal changes its answer: a retry of the same run adopts the first, staged answer
    portal.set_targets("ONC101", "s3://acme-clinops-delivery-dev/studies/ONC101/other/", Y_OK)
    assert h.run_rule("r_portal_lookup")[0].destinations["x"] == X_OK


@pytest.mark.parametrize(
    "x",
    [
        "s3://acme-clinops-delivery-dev/studies/ONC999/in/",
        "s3://evil-bucket/studies/ONC101/in/",
        "sftp://evil.example.com/inbound/ONC101/",
        "s3://acme-clinops-delivery-dev/studies/%2e%2e/in/",
        "https://10.0.0.1/inbound/ONC101/",
    ],
)
def test_attack_destinations_are_refused(portal: MockPortal, harness: Callable[..., OpHarness], x: str) -> None:
    portal.set_targets("ONC101", x, Y_OK)
    h = harness()
    h.run_rule("r_portal_lookup")
    with pytest.raises(SecurityError):
        h.run_rule("r_resolve_destinations")
    rejected = [e for e in h.audit.events if e["type"] == "destination_resolution"]
    assert rejected
    assert all(e["outcome"] == "rejected" for e in rejected)
    assert all(x not in json.dumps(e) for e in rejected)  # only a hash of the URI is audited


def test_answer_for_another_study_is_security(portal: MockPortal, harness: Callable[..., OpHarness]) -> None:
    portal.set_targets("ONC101", X_OK, Y_OK)
    portal.faults = ["wrong_study"]
    with pytest.raises(SecurityError) as ei:
        harness().run_rule("r_portal_lookup")
    assert ei.value.code == "portal_expectation_failed"


def test_missing_required_slot_is_config(portal: MockPortal, harness: Callable[..., OpHarness]) -> None:
    portal.set_targets("ONC101", X_OK, None)
    h = harness()
    h.run_rule("r_portal_lookup")
    with pytest.raises(ConfigError) as ei:
        h.run_rule("r_resolve_destinations")
    assert ei.value.code == "destination_slot_missing"


def test_untrusted_portal_certificate_is_security(portal: MockPortal, harness: Callable[..., OpHarness]) -> None:
    portal.set_targets("ONC101", X_OK, Y_OK)
    with pytest.raises(SecurityError) as ei:
        harness(trust=False).run_rule("r_portal_lookup")
    assert ei.value.code == "tls_untrusted"


def test_rest_api_paging_over_tls(ca: TestCA) -> None:
    integ = IntegrationsFile.model_validate(
        yamlio.to_plain(yamlio.load_path(STARTERS / "rest_to_s3" / "spec" / "integrations.yaml"))
    )
    api = integ.apis[0]
    host = "api.dev.vendor.example"
    with (
        MockApi(ca, host, api_key="synthetic-api-key-1234", pages=[[{"id": "a"}], [{"id": "b"}]]) as srv,
        netmap({(host, 443): ("127.0.0.1", srv.port)}),
    ):

        def key(name: str, *, purpose: str, refresh: bool = False) -> SecretValue:
            return SecretValue("synthetic-api-key-1234", name)

        c = ApiClient(api, f"https://{host}", name="vendor_api", secret=key, extra_ca=ca.cert_pem)
        assert [p.data["items"][0]["id"] for p in c.pages(api.endpoints[0], {})] == ["a", "b"]
        assert all(r.headers.get("x-api-key") == "synthetic-api-key-1234" for r in srv.requests)


def test_mtls_client_certificate_rejection_is_config(ca: TestCA) -> None:
    from factory_spec.sections.integrations import ApiSpec
    from factory_testing.httpserver import Request, Response, StandIn

    class Ok(StandIn):
        def handle(self, req: Request) -> Response:
            return Response(200, {"study_id": "ONC101", "targets": []})

    api = ApiSpec.model_validate(
        {
            "auth": {"type": "mtls", "cert": "client_cert", "key": "client_key"},
            "reliability": {"max_attempts": 1},
            "endpoints": [
                {
                    "id": "delivery_targets",
                    "path": "/api/v1/studies/{study_id}/delivery-targets",
                    "pagination": {"type": "none"},
                    "response": {"fields": {"study_id": {"type": "string"}}},
                    "mapping": {},
                }
            ],
        }
    )
    other = TestCA("Some other CA").client("factory-delivery")
    good = ca.client("factory-delivery")
    host = "api.mtls.acme.example"
    with Ok(ca, (host,), require_client_cert=True) as srv, netmap({(host, 443): ("127.0.0.1", srv.port)}):
        for issued, ok in ((good, True), (other, False)):

            def secret(name: str, *, purpose: str, refresh: bool = False, i: Any = issued) -> SecretValue:
                return SecretValue((i.cert_pem if name == "client_cert" else i.key_pem).decode(), name)

            c = ApiClient(api, f"https://{host}", name="partner_api", secret=secret, extra_ca=ca.cert_pem)
            if ok:
                assert next(iter(c.pages(api.endpoints[0], {"study_id": "ONC101"}))).status == 200
            else:
                with pytest.raises(ConfigError) as ei:
                    list(c.pages(api.endpoints[0], {"study_id": "ONC101"}))
                assert ei.value.code == "tls_client_rejected"
