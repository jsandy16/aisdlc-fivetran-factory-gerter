"""Delivery transports (ADR-0024, P3-3, P3-4): SFTP against an asyncssh stand-in, HTTPS against a TLS sink, S3 in moto.

Every hostname is the real one from the allow-list; only name resolution is redirected (factory_testing.netmap), so
the pinned host key and the TLS certificate are checked against the production names. Synthetic data only.
"""

from __future__ import annotations

import hashlib
import io
import socket
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from factory_core.errors import ConfigError, DataError, SecurityError, TransientError
from factory_runtime.secrets.value import SecretValue
from factory_runtime.tls import http_client, ssl_context
from factory_runtime.transports.common import Source, verified_chunks
from factory_runtime.transports.https import HttpsTarget
from factory_runtime.transports.s3 import AlreadyExists, S3Target, copy_once
from factory_runtime.transports.sftp import SftpTarget
from factory_testing.certs import TestCA
from factory_testing.https_sink import HttpsSink
from factory_testing.keys import (
    CLIENT_KEY,
    DEV_SFTP_HOST,
    TEST_SFTP_HOST,
    openssh_fingerprint,
    private_openssh,
    seeded_ed25519,
)
from factory_testing.netmap import netmap
from factory_testing.sftp_server import SftpServer

DATA = b"PK\x03\x04 synthetic package " + bytes(range(256)) * 300
UPLOAD_HOST = "upload.dev.acme.example"
TOKEN = "synthetic-upload-token-7f3a91"


def source(data: bytes = DATA, *, sha256: str | None = None) -> Source:
    return Source("staging", "run/package.zip", len(data), sha256 or hashlib.sha256(data).hexdigest(),
                  lambda: io.BytesIO(data))  # fmt: skip


def test_verified_chunks_hold_back_the_last_chunk() -> None:
    assert b"".join(verified_chunks(source(), chunk=1000)) == DATA
    sent: list[bytes] = []
    gen = verified_chunks(source(sha256="0" * 64), chunk=1000)
    with pytest.raises(DataError) as ei:
        sent.extend(gen)
    assert ei.value.code == "staged_package_changed"
    assert len(b"".join(sent)) < len(DATA)  # the receiver never gets the whole (wrong) file
    with pytest.raises(DataError):
        list(verified_chunks(Source("b", "k", 0, "0" * 64, lambda: io.BytesIO(b""))))


# ---------------------------------------------------------------- SFTP


def _entry(host: str = DEV_SFTP_HOST, *, pin_name: str = DEV_SFTP_HOST) -> Any:
    return SimpleNamespace(
        host=host, port=22, username="factory-delivery", host_key_algorithm="ssh-ed25519",
        host_key_fingerprint=openssh_fingerprint(seeded_ed25519(pin_name)),
    )  # fmt: skip


def _key(name: str = CLIENT_KEY) -> SecretValue:
    return SecretValue(private_openssh(seeded_ed25519(name)).decode(), "sftp_key")


@pytest.fixture
def sftp(tmp_path: Path) -> Iterator[SftpServer]:
    with SftpServer(tmp_path / "sftp-root") as srv, netmap({(DEV_SFTP_HOST, 22): ("127.0.0.1", srv.port)}):
        yield srv


def _sftp_target(folder: tuple[str, ...] = ("inbound", "ONC101"), **kw: Any) -> SftpTarget:
    args: dict[str, Any] = {"run_tag": "a1b2c3d4e5f6", "literal_root": ("inbound",)}
    args.update(kw)
    return SftpTarget(_entry(), folder, _key(), **args)


def test_sftp_writes_verifies_and_is_idempotent(sftp: SftpServer) -> None:
    t = _sftp_target(("inbound", "ONC101", "2026"))
    try:
        t.ensure_folder()  # creates ONC101/2026 below the approved root
        first = t.put_package("ONC101_PKG0042.zip", source(), verify=True)
        again = t.put_package("ONC101_PKG0042.zip", source(), verify=True)
        assert t.put_small("ONC101_PKG0042.zip.sha256", b"x  ONC101_PKG0042.zip\n", "text/plain") == "written"
    finally:
        t.close()
    assert (first.outcome, first.verification) == ("written", "read_back")
    assert again.outcome == "already_present"
    assert first.target == "sftp://sftp-stub.dev.acme.example/inbound/ONC101/2026/ONC101_PKG0042.zip"
    folder = sftp.file("/inbound/ONC101/2026")
    assert sorted(p.name for p in folder.iterdir()) == ["ONC101_PKG0042.zip", "ONC101_PKG0042.zip.sha256"]
    assert (folder / "ONC101_PKG0042.zip").read_bytes() == DATA
    assert sftp.logins
    assert set(sftp.logins) == {"factory-delivery"}


def test_sftp_existing_file_with_other_content_is_data(sftp: SftpServer) -> None:
    folder = sftp.file("/inbound/ONC101")
    folder.mkdir(parents=True)
    (folder / "pkg.zip").write_bytes(b"someone else's file")
    t = _sftp_target()
    try:
        with pytest.raises(DataError) as ei:
            t.put_package("pkg.zip", source(), verify=True)
    finally:
        t.close()
    assert ei.value.code == "destination_conflict"
    assert (folder / "pkg.zip").read_bytes() == b"someone else's file"  # never replaced
    assert [p.name for p in folder.iterdir()] == ["pkg.zip"]  # the temp file is gone


def test_sftp_changed_staged_package_never_lands(sftp: SftpServer) -> None:
    t = _sftp_target()
    try:
        t.ensure_folder()
        with pytest.raises(DataError) as ei:
            t.put_package("pkg.zip", source(sha256="ab" * 32), verify=True)
    finally:
        t.close()
    assert ei.value.code == "staged_package_changed"
    assert list(sftp.file("/inbound/ONC101").iterdir()) == []


def test_sftp_wrong_host_key_is_security(tmp_path: Path) -> None:
    # the server presents the *test* stub's key; the dev entry pins the dev key
    with (
        SftpServer(tmp_path / "r", host_key=TEST_SFTP_HOST) as srv,
        netmap({(DEV_SFTP_HOST, 22): ("127.0.0.1", srv.port)}),
    ):
        with pytest.raises(SecurityError) as ei:
            _sftp_target()
        assert ei.value.code == "sftp_host_key_mismatch"
        assert srv.logins == []  # the key was refused before authenticating


def test_sftp_refused_key_is_config_and_unreachable_is_transient(sftp: SftpServer) -> None:
    with pytest.raises(ConfigError) as ei:
        SftpTarget(_entry(), ("inbound",), _key("someone-else"), run_tag="x", literal_root=("inbound",))
    assert ei.value.code == "sftp_auth_refused"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    with netmap({(DEV_SFTP_HOST, 22): ("127.0.0.1", closed)}), pytest.raises(TransientError) as te:
        _sftp_target(timeout=5)
    assert te.value.code == "sftp_unavailable"


def test_sftp_folders_only_below_the_approved_root(sftp: SftpServer) -> None:
    outside = _sftp_target(("outbound", "ONC101"))
    try:
        with pytest.raises(SecurityError) as ei:
            outside.ensure_folder()
        assert ei.value.code == "sftp_folder_outside_root"
    finally:
        outside.close()
    no_root = _sftp_target(("partners", "ONC101"), literal_root=("partners",))
    try:
        with pytest.raises(ConfigError) as ce:
            no_root.ensure_folder()  # the approved root itself is never created
        assert ce.value.code == "destination_folder_missing"
    finally:
        no_root.close()
    assert not sftp.file("/partners").exists()
    assert not sftp.file("/outbound").exists()


# ---------------------------------------------------------------- HTTPS


@pytest.fixture
def ca() -> TestCA:
    return TestCA()


@pytest.fixture
def sink(ca: TestCA) -> Iterator[HttpsSink]:
    s = HttpsSink(ca, UPLOAD_HOST, token=TOKEN)
    s.start()
    with netmap({(UPLOAD_HOST, 443): ("127.0.0.1", s.port)}):
        yield s
    s.stop()


def _https(ca: TestCA | None, *, token: str = TOKEN, client_cert: Any = None, method: str = "PUT") -> HttpsTarget:
    entry = SimpleNamespace(host=UPLOAD_HOST, port=443, method=method, write_once="if_none_match")
    ctx = ssl_context(extra_ca=ca.cert_pem if ca else None, client_cert=client_cert)
    bearer = SecretValue(token, "upload_token") if token else None
    return HttpsTarget(entry, ("api", "v2", "studies", "ONC101", "packages"), http_client(ctx), bearer)


PATH = "/api/v2/studies/ONC101/packages/pkg.zip"


@pytest.mark.parametrize(
    ("digest", "allow_get", "allow_head", "expected"),
    [
        (True, True, True, "digest"),
        (False, True, True, "read_back"),
        (False, False, True, "size_only"),
        (False, False, False, "none"),
    ],
)
def test_https_best_available_verification(
    sink: HttpsSink, ca: TestCA, digest: bool, allow_get: bool, allow_head: bool, expected: str
) -> None:
    sink.digest, sink.allow_get, sink.allow_head = digest, allow_get, allow_head
    t = _https(ca)
    try:
        r = t.put_package("pkg.zip", source(), verify=True)
    finally:
        t.close()
    assert (r.outcome, r.verification) == ("written", expected)
    assert sink.files[PATH] == DATA
    put = next(q for q in sink.requests if q.method == "PUT")
    assert put.headers["if-none-match"] == "*"
    assert put.headers["repr-digest"].startswith("sha-256=:")
    assert put.headers["authorization"] == f"Bearer {TOKEN}"


def test_https_existing_file_is_compared_before_sending(sink: HttpsSink, ca: TestCA) -> None:
    sink.files[PATH] = DATA
    t = _https(ca)
    try:
        assert t.put_package("pkg.zip", source(), verify=True).outcome == "already_present"
        sink.files[PATH] = b"other content"
        with pytest.raises(DataError) as ei:
            t.put_package("pkg.zip", source(), verify=True)
    finally:
        t.close()
    assert ei.value.code == "destination_conflict"
    assert not any(q.method == "PUT" for q in sink.requests)  # HEAD first: nothing was sent


class RacingSink(HttpsSink):
    """Another writer stores the file between our HEAD and our PUT."""

    racer: bytes = DATA

    def handle(self, req: Any) -> Any:
        if req.method == "PUT" and req.path not in self.files:
            self.files[req.path] = self.racer
        return super().handle(req)


@pytest.mark.parametrize(("racer", "outcome"), [(DATA, "already_present"), (b"the other writer's file", None)])
def test_https_412_is_compared(ca: TestCA, racer: bytes, outcome: str | None) -> None:
    s = RacingSink(ca, UPLOAD_HOST, token=TOKEN)
    s.racer = racer
    with s, netmap({(UPLOAD_HOST, 443): ("127.0.0.1", s.port)}):
        t = _https(ca)
        try:
            if outcome is None:
                with pytest.raises(DataError) as ei:
                    t.put_package("pkg.zip", source(), verify=True)
                assert ei.value.code == "destination_conflict"
            else:
                assert t.put_package("pkg.zip", source(), verify=True).outcome == outcome
        finally:
            t.close()
        assert s.files[PATH] == racer  # never overwritten


def test_https_failures_are_classified(sink: HttpsSink, ca: TestCA) -> None:
    t = _https(ca, token="wrong-token-0000")
    with pytest.raises(ConfigError) as ce:
        t.put_package("pkg.zip", source(), verify=True)
    assert ce.value.code == "delivery_auth_refused"
    t.close()
    sink.fail_puts = 1
    t = _https(ca)
    with pytest.raises(TransientError) as te:
        t.put_package("pkg.zip", source(), verify=True)
    assert te.value.code == "https_unavailable"
    sink.corrupt = True  # stores something else than it received
    with pytest.raises(DataError) as de:
        t.put_package("pkg.zip", source(), verify=True)
    assert de.value.code == "delivery_verification_failed"
    t.close()
    t = _https(None)  # the stand-in's CA is not trusted
    with pytest.raises(SecurityError) as se:
        t.put_package("pkg2.zip", source(), verify=True)
    assert se.value.code == "tls_untrusted"
    t.close()


def test_https_changed_staged_package_is_never_stored(sink: HttpsSink, ca: TestCA) -> None:
    t = _https(ca)
    try:
        with pytest.raises(DataError) as ei:
            t.put_package("pkg.zip", source(sha256="cd" * 32), verify=True)
    finally:
        t.close()
    assert ei.value.code == "staged_package_changed"
    assert PATH not in sink.files


def test_https_mtls(ca: TestCA) -> None:
    s = HttpsSink(ca, UPLOAD_HOST, require_client_cert=True)
    with s, netmap({(UPLOAD_HOST, 443): ("127.0.0.1", s.port)}):
        issued = ca.client("factory-delivery")
        cert = (SecretValue(issued.cert_pem.decode(), "cert"), SecretValue(issued.key_pem.decode(), "key"))
        t = _https(ca, token="", client_cert=cert)
        try:
            assert t.put_package("pkg.zip", source(), verify=True).outcome == "written"
        finally:
            t.close()
        t = _https(ca, token="")
        try:
            with pytest.raises(ConfigError) as ei:
                t.put_package("pkg2.zip", source(), verify=True)
            assert ei.value.code == "tls_client_rejected"
        finally:
            t.close()


# ---------------------------------------------------------------- S3


@pytest.fixture
def buckets(aws: None) -> Any:
    s3 = boto3.client("s3", region_name="eu-west-1")
    for b in ("staging", "delivery"):
        s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    s3.put_object(Bucket="staging", Key="run/package.zip", Body=DATA)
    return s3


def _s3_source(s3: Any, data: bytes = DATA) -> Source:
    def opener() -> Any:
        return io.BytesIO(s3.get_object(Bucket="staging", Key="run/package.zip")["Body"].read())

    return Source("staging", "run/package.zip", len(data), hashlib.sha256(data).hexdigest(), opener)


def test_s3_target_copies_write_once_with_the_entry_key(buckets: Any) -> None:
    s3 = buckets
    t = S3Target(s3, "delivery", ("studies", "ONC101", "in"), "arn:aws:kms:eu-west-1:111122223333:alias/x", None)
    r = t.put_package("pkg.zip", _s3_source(s3), verify=True)
    assert (r.outcome, r.verification, r.target) == ("written", "read_back", "s3://delivery/studies/ONC101/in/pkg.zip")
    head = s3.head_object(Bucket="delivery", Key="studies/ONC101/in/pkg.zip")
    assert head["ServerSideEncryption"] == "aws:kms"
    assert head["SSEKMSKeyId"].endswith("alias/x")
    assert t.put_package("pkg.zip", _s3_source(s3), verify=True).outcome == "already_present"
    assert t.put_small("pkg.zip.md5", b"abc  pkg.zip\n", "text/plain") == "written"
    assert t.put_small("pkg.zip.md5", b"abc  pkg.zip\n", "text/plain") == "already_present"
    with pytest.raises(DataError):
        t.put_small("pkg.zip.md5", b"def  pkg.zip\n", "text/plain")
    s3.put_object(Bucket="delivery", Key="studies/ONC101/in/other.zip", Body=b"not ours")
    with pytest.raises(DataError) as ei:
        t.put_package("other.zip", _s3_source(s3), verify=True)
    assert ei.value.code == "destination_conflict"


class Spy:
    """Delegates to the moto client; records UploadPartCopy calls and can answer them with 412."""

    def __init__(self, s3: Any) -> None:
        self.s3 = s3
        self.part_calls: list[dict[str, Any]] = []
        self.fail_parts = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.s3, name)

    def upload_part_copy(self, **kw: Any) -> Any:
        from botocore.exceptions import ClientError

        self.part_calls.append(kw)
        if self.fail_parts:
            err = {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}}
            raise ClientError(err, "UploadPartCopy")  # type: ignore[arg-type]
        return self.s3.upload_part_copy(**kw)


def test_copy_once_is_multipart_conditional_and_write_once(buckets: Any) -> None:
    s3 = buckets
    big = bytes(range(256)) * (11 * 4096)  # 11 MiB: three 5 MiB parts
    s3.put_object(Bucket="staging", Key="big.bin", Body=big)
    etag = s3.head_object(Bucket="staging", Key="big.bin")["ETag"]
    copy_once(s3, "staging", "big.bin", "delivery", "copies/big.bin", size=len(big), src_etag=etag,
              part_size=5 * 1024 * 1024, metadata={"factory-source-etag": etag.strip('"')})  # fmt: skip
    got = s3.get_object(Bucket="delivery", Key="copies/big.bin")
    assert got["Body"].read() == big
    assert got["Metadata"]["factory-source-etag"] == etag.strip('"')
    with pytest.raises(AlreadyExists):
        copy_once(s3, "staging", "big.bin", "delivery", "copies/big.bin", size=len(big), src_etag=etag)
    # every part is conditional on the listed ETag (moto does not evaluate the condition, real S3 does: spy + 412)
    spy = Spy(s3)
    copy_once(spy, "staging", "big.bin", "delivery", "copies/two.bin", size=len(big), src_etag=etag,
              part_size=5 * 1024 * 1024)  # fmt: skip
    assert [c["CopySourceIfMatch"] for c in spy.part_calls] == [etag] * 3
    assert [c["CopySourceRange"] for c in spy.part_calls][-1] == f"bytes={10 * 1024 * 1024}-{len(big) - 1}"
    spy.fail_parts = True
    with pytest.raises(DataError) as ei:
        copy_once(spy, "staging", "big.bin", "delivery", "copies/new.bin", size=len(big), src_etag=etag)
    assert ei.value.code == "source_changed"
    assert s3.list_multipart_uploads(Bucket="delivery").get("Uploads", []) == []  # aborted, nothing left behind
    assert "copies/new.bin" not in [o["Key"] for o in s3.list_objects_v2(Bucket="delivery")["Contents"]]
    copy_once(s3, "staging", "big.bin", "delivery", "copies/empty.bin", size=0)
    assert s3.get_object(Bucket="delivery", Key="copies/empty.bin")["Body"].read() == b""


def test_kms_key_and_owner_per_placement(workdir: Path) -> None:
    from factory_runtime.delivery import kms_key_id, placement_account
    from rt_helpers import make_config

    cfg = make_config(workdir, "study_pkg_delivery", placement_accounts={"cro_archive": "444455556666"})
    team = SimpleNamespace(placement="team", kms_key="alias/clinops-delivery-dev")
    other = SimpleNamespace(placement="cro_archive", kms_key="alias/cro-archive-inbound")
    assert kms_key_id(cfg, team) == "alias/clinops-delivery-dev"  # type: ignore[arg-type]
    assert placement_account(cfg, team) is None  # type: ignore[arg-type]
    assert kms_key_id(cfg, other) == "arn:aws:kms:eu-west-1:444455556666:alias/cro-archive-inbound"  # type: ignore[arg-type]
    assert placement_account(cfg, other) == "444455556666"  # type: ignore[arg-type]
    unknown = SimpleNamespace(placement="nowhere", kms_key="alias/x")
    with pytest.raises(ConfigError) as ei:
        kms_key_id(cfg, unknown)  # type: ignore[arg-type]
    assert ei.value.code == "placement_unknown"
