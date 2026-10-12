"""FTP / FTPS (explicit TLS, encrypted data channel) via ftplib."""
from __future__ import annotations

import calendar
import ftplib
import ssl
import posixpath
import time

from ..errors import ConfigError, TransientError
from . import RemoteFile
from .tlsx import verifying_context


def _parse_mdtm(value: str) -> float:
    value = value.split(".")[0]
    return float(calendar.timegm(time.strptime(value, "%Y%m%d%H%M%S")))


class FtpSession:
    def __init__(self, source: dict, configuration: dict):
        self.root = source.get("root", "/").rstrip("/") or "/"
        pw = configuration.get(source["password"]["secret"])
        if not pw:
            raise ConfigError(f"configuration key '{source['password']['secret']}' is missing or empty")
        tls = source.get("tls", "explicit") == "explicit"
        timeout = source.get("timeout_seconds", 60)
        # FTP_TLS() without a context does NOT verify the server certificate: always pass a verifying one
        self._ftp = ftplib.FTP_TLS(timeout=timeout, context=verifying_context(source, configuration)) if tls else ftplib.FTP(timeout=timeout)
        try:
            self._ftp.connect(source["host"], int(source.get("port", 21)))
            self._ftp.login(source["username"], pw)
            if tls:
                self._ftp.prot_p()  # encrypt the data channel too
            self._ftp.set_pasv(bool(source.get("passive", True)))
        except ftplib.error_perm as exc:
            raise ConfigError(f"FTP login refused ({str(exc)[:3]})") from None
        except ssl.SSLCertVerificationError as exc:
            raise ConfigError(f"FTPS server certificate is not trusted ({exc.verify_message})") from None
        except (OSError, EOFError, ftplib.error_temp) as exc:
            raise TransientError(f"FTP connect failed ({type(exc).__name__})") from None

    def _walk_mlsd(self) -> list[RemoteFile]:
        out: list[RemoteFile] = []
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            abs_dir = posixpath.join(self.root, rel_dir) if rel_dir else self.root
            for name, facts in self._ftp.mlsd(abs_dir, facts=["type", "size", "modify"]):
                if name in (".", ".."):
                    continue
                rel = posixpath.join(rel_dir, name) if rel_dir else name
                kind = facts.get("type", "")
                if kind == "dir":
                    stack.append(rel)
                elif kind == "file":
                    mtime = _parse_mdtm(facts["modify"]) if "modify" in facts else 0.0
                    out.append(RemoteFile(rel, int(facts.get("size", 0)), mtime))
        return out

    def list_files(self) -> list[RemoteFile]:
        try:
            files = self._walk_mlsd()
        except ftplib.error_perm:  # server without MLSD: flat NLST + SIZE/MDTM
            files = []
            for name in self._ftp.nlst(self.root):
                base = posixpath.basename(name)
                rel = base
                path = posixpath.join(self.root, base)
                try:
                    size = self._ftp.size(path) or 0
                    mtime = _parse_mdtm(self._ftp.sendcmd(f"MDTM {path}").split()[1])
                except ftplib.error_perm:
                    continue  # a directory
                files.append(RemoteFile(rel, int(size), mtime))
        return sorted(files, key=lambda f: f.path)

    def download(self, rel: str, dest: str) -> None:
        with open(dest, "wb") as fh:
            self._ftp.retrbinary(f"RETR {posixpath.join(self.root, rel)}", fh.write)

    def read_text(self, rel: str, limit: int = 1 << 20) -> str | None:
        chunks: list[bytes] = []
        try:
            self._ftp.retrbinary(f"RETR {posixpath.join(self.root, rel)}", chunks.append)
        except ftplib.error_perm:
            return None
        return b"".join(chunks)[:limit].decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._ftp.quit()
        except Exception:  # noqa: BLE001
            self._ftp.close()
