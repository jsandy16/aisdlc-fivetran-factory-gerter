"""Deterministic (seeded) SSH keys for the stand-ins.

The worked example pins the host-key fingerprints of its dev/test SFTP stubs. The matching private keys are derived
here from a fixed seed at test time, so no private key is ever committed. Synthetic only: never use outside tests.
"""

from __future__ import annotations

import base64
import hashlib
import struct

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def seeded_ed25519(name: str) -> Ed25519PrivateKey:
    """The same key for the same name, on every machine."""
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"factory-testing/ssh/" + name.encode()).digest())


def _ssh_string(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def public_blob(key: Ed25519PrivateKey) -> bytes:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _ssh_string(b"ssh-ed25519") + _ssh_string(raw)


def openssh_fingerprint(key: Ed25519PrivateKey) -> str:
    """'SHA256:<base64 without padding>', as printed by ssh-keygen -l and paramiko PKey.fingerprint."""
    digest = hashlib.sha256(public_blob(key)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def private_openssh(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    )


def public_openssh(key: Ed25519PrivateKey) -> str:
    return "ssh-ed25519 " + base64.b64encode(public_blob(key)).decode()


# names used by the worked example's dev/test SFTP stubs and the delivery client key
DEV_SFTP_HOST = "sftp-stub.dev.acme.example"
TEST_SFTP_HOST = "sftp-stub.test.acme.example"
CLIENT_KEY = "factory-delivery-client"
