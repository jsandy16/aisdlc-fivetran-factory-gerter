"""A local SFTP server for tests (asyncssh): seeded host key, one authorised client key and user, a chroot folder.

asyncssh implements the standard SFTP RENAME as "fail if the new name exists" (like OpenSSH's sftp-server), which is
what the runtime's no-clobber delivery relies on. Files live in a real temporary folder, so tests can pre-place
files or inspect what was delivered. Synthetic only.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

import asyncssh

from .keys import CLIENT_KEY, DEV_SFTP_HOST, private_openssh, public_openssh, seeded_ed25519

logging.getLogger("asyncssh").setLevel(logging.WARNING)


class SftpServer:
    def __init__(
        self,
        root: Path,
        *,
        host_key: str = DEV_SFTP_HOST,
        client_key: str = CLIENT_KEY,
        username: str = "factory-delivery",
        folders: tuple[str, ...] = ("inbound",),
    ) -> None:
        self.root = root
        self.host_key = host_key
        self.client_key = client_key
        self.username = username
        self.logins: list[str] = []
        self._conns: list[Any] = []
        for f in folders:
            (root / f).mkdir(parents=True, exist_ok=True)
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._acceptor: Any = None

    async def _listen(self) -> Any:
        outer = self
        allowed = asyncssh.import_public_key(public_openssh(seeded_ed25519(self.client_key)))
        root = str(self.root).encode()

        class Server(asyncssh.SSHServer):
            def connection_made(self, conn: Any) -> None:
                outer._conns.append(conn)

            def begin_auth(self, username: str) -> bool:
                return True

            def public_key_auth_supported(self) -> bool:
                return True

            def validate_public_key(self, username: str, key: Any) -> bool:
                ok = bool(username == outer.username and key.public_data == allowed.public_data)
                if ok:
                    outer.logins.append(username)
                return ok

        class Chroot(asyncssh.SFTPServer):
            def __init__(self, chan: Any) -> None:
                super().__init__(chan, chroot=root)

        return await asyncssh.listen(
            "127.0.0.1",
            0,
            server_factory=Server,
            server_host_keys=[asyncssh.import_private_key(private_openssh(seeded_ed25519(self.host_key)))],
            sftp_factory=Chroot,
            allow_scp=False,
            agent_forwarding=False,
            x11_forwarding=False,
            allow_pty=False,
        )

    def start(self) -> SftpServer:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="sftp-standin")
        self._thread.start()
        self._acceptor = asyncio.run_coroutine_threadsafe(self._listen(), self._loop).result(30)
        self.port = int(self._acceptor.sockets[0].getsockname()[1])
        return self

    def stop(self) -> None:
        if self._loop is None:
            return

        async def close() -> None:
            self._acceptor.close()
            for conn in self._conns:
                conn.close()
            await asyncio.wait_for(self._acceptor.wait_closed(), 10)

        asyncio.run_coroutine_threadsafe(close(), self._loop).result(30)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(30)
        self._loop.close()
        self._loop = None

    def file(self, path: str) -> Path:
        """The local file behind a server path such as /inbound/ONC101/x.zip."""
        return self.root / path.lstrip("/")

    def __enter__(self) -> SftpServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
