"""SFTP delivery (ADR-0024): pinned host key, no-clobber writes, verified after writing.

- The server's host key must have the pinned SHA-256 fingerprint (constant-time compare); only the pinned key type is
  negotiated; no system known_hosts, no agent, no keys from disk. A different key is a security failure (77).
- Files are written to a unique temporary name in the target folder (created exclusively, so concurrent attempts
  never touch each other's data), then renamed with the standard SFTP RENAME, which refuses to replace an existing
  file. When the final name exists, its SHA-256 is compared: equal means an earlier attempt delivered it.
- Missing folders are created only below the allow-list entry's fixed root (decision P3-4).
- Verification after writing: size and a full read-back SHA-256 (size only when verify_after_write is off).
"""

from __future__ import annotations

import hashlib
import hmac
import io
import posixpath
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import paramiko
from factory_core.errors import ConfigError, DataError, SecurityError, TransientError

from ..secrets.value import SecretValue
from .common import CHUNK, Outcome, PutResult, Source, verified_chunks

_ALL_KEY_TYPES = tuple(paramiko.Transport._preferred_keys)


class PinnedHostKey(paramiko.MissingHostKeyPolicy):  # type: ignore[misc]  # paramiko ships no type hints
    """Accept exactly one host key: the pinned fingerprint. No known_hosts file is ever read or written."""

    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint

    def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
        if not hmac.compare_digest(str(key.fingerprint), self.fingerprint):
            raise SecurityError("sftp_host_key_mismatch", f"the host key of {hostname} is not the pinned key")


def _load_private_key(secret: SecretValue) -> Any:
    with secret.reveal() as pem:
        for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                return cls.from_private_key(io.StringIO(pem))
            except (paramiko.SSHException, ValueError):
                continue
    raise ConfigError("sftp_key_invalid", "the SFTP private key cannot be read")


@contextmanager
def _mapped(host: str) -> Iterator[None]:
    """SFTP status errors to the exit-code contract: permission problems are configuration, the rest transient."""
    try:
        yield
    except PermissionError as exc:
        raise ConfigError("sftp_permission_denied", f"{host} refused the operation ({exc.strerror})") from None
    except (OSError, EOFError, paramiko.SSHException) as exc:
        raise TransientError("sftp_write_failed", f"writing to {host} failed ({type(exc).__name__})") from exc


class SftpTarget:
    def __init__(
        self,
        entry: Any,
        folder: tuple[str, ...],
        private_key: SecretValue,
        *,
        run_tag: str,
        literal_root: tuple[str, ...],
        timeout: float = 30.0,
    ) -> None:
        self.entry = entry
        self.folder = "/" + "/".join(folder)
        self.literal_root = "/" + "/".join(literal_root)
        self.run_tag = run_tag
        disabled = [k for k in _ALL_KEY_TYPES if k != entry.host_key_algorithm]
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(PinnedHostKey(entry.host_key_fingerprint))
        try:
            self.client.connect(
                hostname=entry.host,
                port=entry.port,
                username=entry.username,
                pkey=_load_private_key(private_key),
                look_for_keys=False,
                allow_agent=False,
                timeout=timeout,
                banner_timeout=timeout,
                auth_timeout=timeout,
                disabled_algorithms={"keys": disabled},
            )
            sftp = self.client.open_sftp()
        except (SecurityError, ConfigError):
            self.client.close()
            raise
        except paramiko.BadHostKeyException:
            self.client.close()
            raise SecurityError("sftp_host_key_mismatch", f"the host key of {entry.host} changed") from None
        except paramiko.AuthenticationException:
            self.client.close()
            raise ConfigError("sftp_auth_refused", f"{entry.host} refused the delivery account's key") from None
        except (paramiko.SSHException, OSError, EOFError) as exc:
            self.client.close()
            raise TransientError("sftp_unavailable", f"cannot reach {entry.host} ({type(exc).__name__})") from exc
        self.sftp = sftp
        channel = sftp.get_channel()
        if channel is not None:
            channel.settimeout(timeout)

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------ folders

    def _exists(self, path: str) -> bool:
        try:
            self.sftp.stat(path)
            return True
        except FileNotFoundError:
            return False

    def ensure_folder(self) -> None:
        """Create the missing folders between the entry's fixed root and the target folder (never the root itself)."""
        root = self.literal_root.rstrip("/")
        if not (self.folder == root or self.folder.startswith(root + "/")):
            raise SecurityError("sftp_folder_outside_root", "the target folder is outside the approved root")
        with _mapped(self.entry.host):
            if not self._exists(root):
                raise ConfigError("destination_folder_missing", "the approved root folder does not exist on the server")
            path = root
            for part in self.folder[len(root) :].strip("/").split("/"):
                if not part:
                    continue
                path = f"{path}/{part}"
                try:
                    self.sftp.mkdir(path)
                except OSError:
                    if not self._exists(path):  # another attempt may have created it meanwhile
                        raise

    # ------------------------------------------------------------------ files

    def _sha256(self, path: str) -> tuple[int, str]:
        h = hashlib.sha256()
        n = 0
        with self.sftp.open(path, "rb") as fh:
            fh.prefetch()
            while chunk := fh.read(CHUNK):
                h.update(chunk)
                n += len(chunk)
        return n, h.hexdigest()

    def _put(self, name: str, chunks: Iterator[bytes], size: int, sha256: str) -> Outcome:
        final = posixpath.join(self.folder, name)
        tmp = posixpath.join(self.folder, f".{name}.{self.run_tag}.{uuid.uuid4().hex[:8]}.part")
        try:
            with _mapped(self.entry.host):
                with self.sftp.open(tmp, "wxb") as out:  # x: O_EXCL, never another attempt's file
                    out.set_pipelined(True)
                    for chunk in chunks:
                        out.write(chunk)
                try:
                    self.sftp.rename(tmp, final)  # standard RENAME: fails when `final` exists
                    return "written"
                except OSError:
                    if not self._exists(final):
                        raise
                got_size, got = self._sha256(final)
            if got_size == size and hmac.compare_digest(got, sha256):
                return "already_present"
            raise DataError("destination_conflict", f"{name} already exists at the destination with other content")
        finally:
            try:
                if self._exists(tmp):
                    self.sftp.remove(tmp)
            except (OSError, EOFError, paramiko.SSHException):
                pass  # the connection is gone; the temp name is unique and never read

    def put_package(self, name: str, src: Source, *, verify: bool) -> PutResult:
        outcome = self._put(name, verified_chunks(src), src.size, src.sha256)
        final = posixpath.join(self.folder, name)
        if verify:
            with _mapped(self.entry.host):
                got_size, got = self._sha256(final)
            if got_size != src.size or not hmac.compare_digest(got, src.sha256):
                raise DataError("delivery_verification_failed", f"{name} at the destination differs from the package")
            return PutResult(outcome, "read_back", self.uri(name))
        with _mapped(self.entry.host):
            stored = self.sftp.stat(final).st_size
        if stored != src.size:
            raise DataError("delivery_verification_failed", f"{name} at the destination has the wrong size")
        return PutResult(outcome, "size_only", self.uri(name))

    def put_small(self, name: str, data: bytes, content_type: str) -> Outcome:
        return self._put(name, iter([data]), len(data), hashlib.sha256(data).hexdigest())

    def uri(self, name: str) -> str:
        port = "" if self.entry.port == 22 else f":{self.entry.port}"
        return f"sftp://{self.entry.host}{port}{posixpath.join(self.folder, name)}"
