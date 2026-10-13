"""SFTP via paramiko, with a pinned host key (connections to an unknown or changed key are refused)."""
from __future__ import annotations

import base64
import io
import posixpath
import stat as statmod

from ..errors import ConfigError, TransientError
from . import RemoteFile


def _host_key(spec: str):
    import paramiko

    parts = spec.split()
    if len(parts) < 2:
        raise ConfigError("source.host_key must look like '<type> <base64>'")
    try:
        return paramiko.PKey.from_type_string(parts[0], base64.b64decode(parts[1]))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"source.host_key is not a valid public key ({type(exc).__name__})") from None


def _private_key(pem: str):
    import paramiko

    errors = []
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(pem))
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)
    raise ConfigError("SFTP private key could not be parsed (" + ", ".join(errors) + ")")


class SftpSession:
    def __init__(self, source: dict, configuration: dict):
        import paramiko

        self.root = source.get("root", "/").rstrip("/") or "/"
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.RejectPolicy())
        port = int(source.get("port", 22))
        hostname = source["host"] if port == 22 else f"[{source['host']}]:{port}"
        key = _host_key(source["host_key"])
        self._client.get_host_keys().add(hostname, key.get_name(), key)
        kwargs = dict(hostname=source["host"], port=port, username=source["username"], timeout=source.get("timeout_seconds", 60),
                      banner_timeout=source.get("timeout_seconds", 60), auth_timeout=source.get("timeout_seconds", 60),
                      allow_agent=False, look_for_keys=False)
        if source.get("private_key"):
            pem = configuration.get(source["private_key"]["secret"])
            if not pem:
                raise ConfigError(f"configuration key '{source['private_key']['secret']}' is missing or empty")
            kwargs["pkey"] = _private_key(pem)
        else:
            pw = configuration.get(source["password"]["secret"])
            if not pw:
                raise ConfigError(f"configuration key '{source['password']['secret']}' is missing or empty")
            kwargs["password"] = pw
        try:
            self._client.connect(**kwargs)
        except paramiko.BadHostKeyException:
            raise ConfigError("SFTP host key does not match the pinned source.host_key") from None
        except paramiko.SSHException as exc:
            if "not found in known_hosts" in str(exc) or "Server" in str(exc) and "not found" in str(exc):
                raise ConfigError("SFTP host key is not the pinned source.host_key") from None
            if isinstance(exc, paramiko.AuthenticationException):
                raise ConfigError("SFTP authentication failed") from None
            raise TransientError(f"SFTP connect failed ({type(exc).__name__})") from None
        except OSError as exc:
            raise TransientError(f"SFTP connect failed ({type(exc).__name__})") from None
        self._sftp = self._client.open_sftp()
        self._sftp.get_channel().settimeout(source.get("timeout_seconds", 60))

    def list_files(self) -> list[RemoteFile]:
        out: list[RemoteFile] = []
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            abs_dir = posixpath.join(self.root, rel_dir) if rel_dir else self.root
            for e in self._sftp.listdir_attr(abs_dir):
                rel = posixpath.join(rel_dir, e.filename) if rel_dir else e.filename
                if statmod.S_ISDIR(e.st_mode or 0):
                    stack.append(rel)
                elif statmod.S_ISREG(e.st_mode or 0):
                    out.append(RemoteFile(rel, int(e.st_size or 0), float(e.st_mtime or 0)))
                elif statmod.S_ISLNK(e.st_mode or 0):  # a link to a file is a delivery like any other (directory links are not followed)
                    try:
                        t = self._sftp.stat(posixpath.join(abs_dir, e.filename))
                    except OSError:
                        out.append(RemoteFile(rel, 0, float(e.st_mtime or 0)))  # dangling: surfaces as a failed download, not silence
                        continue
                    if statmod.S_ISREG(t.st_mode or 0):
                        out.append(RemoteFile(rel, int(t.st_size or 0), float(t.st_mtime or 0)))
        return sorted(out, key=lambda f: f.path)

    def download(self, rel: str, dest: str) -> None:
        self._sftp.get(posixpath.join(self.root, rel), dest)

    def read_text(self, rel: str, limit: int = 1 << 20) -> str | None:
        try:
            with self._sftp.open(posixpath.join(self.root, rel), "rb") as fh:
                return fh.read(limit).decode("utf-8", errors="replace")
        except FileNotFoundError:
            return None
        except IOError:
            return None

    def close(self) -> None:
        try:
            self._sftp.close()
        finally:
            self._client.close()
