"""A throwaway test certificate authority: server certificates for DNS names, client certificates for mTLS.

Certificates carry key identifiers, so they pass the strict X.509 checks Python 3.13 enables by default.

Generated per test session (never committed). Synthetic only.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@dataclass(frozen=True)
class Issued:
    cert_pem: bytes
    key_pem: bytes


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


class TestCA:
    __test__ = False  # not a pytest class

    def __init__(self, name: str = "Pipeline Factory Test CA") -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.UTC)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
            .sign(self.key, hashes.SHA256())
        )

    @property
    def cert_pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    def _issue(self, cn: str, sans: list[x509.GeneralName], usage: x509.ObjectIdentifier) -> Issued:
        key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
            .issuer_name(self.cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False)
            .sign(self.key, hashes.SHA256())
        )
        return Issued(cert.public_bytes(serialization.Encoding.PEM), _key_pem(key))

    def server(self, *hostnames: str) -> Issued:
        sans: list[x509.GeneralName] = []
        for h in hostnames:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(h)))
            except ValueError:
                sans.append(x509.DNSName(h))
        return self._issue(hostnames[0], sans, ExtendedKeyUsageOID.SERVER_AUTH)

    def client(self, name: str) -> Issued:
        return self._issue(name, [x509.DNSName(name)], ExtendedKeyUsageOID.CLIENT_AUTH)
