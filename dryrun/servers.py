"""Local stand-ins for the dry run: SFTP (asyncssh), FTPS (pyftpdlib + TLS), S3 (moto server) and Postgres 16.

Nothing here is used in production. Each server binds to 127.0.0.1 only. Keys and certificates are generated
fresh into the state folder on every start.

    python dryrun/servers.py <state_dir>     # starts everything, writes <state_dir>/env.json, blocks
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

PG_BIN = Path(os.environ.get("PF_PG_BIN", "/usr/lib/postgresql/16/bin"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sftp_server(state: Path, port: int, ready: threading.Event) -> None:
    import asyncssh

    keys = state / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    host = asyncssh.generate_private_key("ssh-ed25519")
    client = asyncssh.generate_private_key("ssh-ed25519")
    host.write_private_key(str(keys / "host_ed25519"))
    client.write_private_key(str(keys / "client_ed25519"))
    (keys / "host_ed25519.pub").write_bytes(host.export_public_key())
    (keys / "authorized_keys").write_bytes(client.export_public_key())
    root = state / "sftp"
    root.mkdir(exist_ok=True)

    class Chroot(asyncssh.SFTPServer):
        def __init__(self, chan):
            super().__init__(chan, chroot=str(root).encode())

    async def main():
        await asyncssh.listen("127.0.0.1", port, server_host_keys=[str(keys / "host_ed25519")],
                              authorized_client_keys=str(keys / "authorized_keys"), sftp_factory=Chroot,
                              allow_scp=False)
        ready.set()
        await asyncio.Event().wait()

    asyncio.run(main())


def ftps_server(state: Path, port: int, ready: threading.Event) -> None:
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import TLS_FTPHandler
    from pyftpdlib.servers import FTPServer

    cert = state / "ftps.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=127.0.0.1",
                    "-keyout", str(cert), "-out", str(cert)], check=True, capture_output=True)
    root = state / "ftp"
    root.mkdir(exist_ok=True)
    auth = DummyAuthorizer()
    auth.add_user("pf_ftp", "pf-ftp-password", str(root), perm="elr")
    TLS_FTPHandler.certfile = str(cert)
    TLS_FTPHandler.authorizer = auth
    TLS_FTPHandler.tls_control_required = True
    TLS_FTPHandler.tls_data_required = True
    TLS_FTPHandler.passive_ports = range(60000, 60100)
    srv = FTPServer(("127.0.0.1", port), TLS_FTPHandler)
    ready.set()
    srv.serve_forever()


def s3_server(port: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "moto.server", "-H", "127.0.0.1", "-p", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def postgres(state: Path, port: int) -> dict:
    import tempfile

    run_as = [] if os.geteuid() != 0 else ["runuser", "-u", "postgres", "--"]  # postgres refuses to run as root
    base = Path(tempfile.mkdtemp(prefix="pf-dryrun-pg-")) if run_as else state  # must be reachable by that user
    data = base / "pg"
    pw = base / "pgpw"
    pw.write_text("pf-pg-admin\n")
    if run_as:
        os.chmod(base, 0o700)
        shutil.chown(base, "postgres")
        shutil.chown(pw, "postgres")
    state = base
    subprocess.run(run_as + [str(PG_BIN / "initdb"), "-D", str(data), "-U", "pf_admin", "--pwfile", str(pw), "-A", "scram-sha-256"],
                   check=True, capture_output=True)
    subprocess.run(run_as + [str(PG_BIN / "pg_ctl"), "-D", str(data), "-o", f"-p {port} -k {state} -c listen_addresses=127.0.0.1",
                             "-l", str(state / "pg.log"), "-w", "start"], check=True, capture_output=True)
    return {"port": port, "admin_user": "pf_admin", "admin_password": "pf-pg-admin", "data": str(data), "run_as": run_as}


def wait_port(port: int, timeout: float = 20) -> None:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"port {port} did not open")


def main(state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o755)
    ports = {"sftp": free_port(), "ftp": free_port(), "s3": free_port(), "pg": free_port()}
    ev1, ev2 = threading.Event(), threading.Event()
    threading.Thread(target=sftp_server, args=(state, ports["sftp"], ev1), daemon=True).start()
    threading.Thread(target=ftps_server, args=(state, ports["ftp"], ev2), daemon=True).start()
    s3 = s3_server(ports["s3"])
    pg = postgres(state, ports["pg"])
    ev1.wait(20)
    ev2.wait(20)
    for p in ports.values():
        wait_port(p)
    env = {"ports": ports, "sftp_root": str(state / "sftp"), "ftp_root": str(state / "ftp"),
           "host_key": (state / "keys" / "host_ed25519.pub").read_text().split()[0] + " " +
                       (state / "keys" / "host_ed25519.pub").read_text().split()[1],
           "client_key": str(state / "keys" / "client_ed25519"), "ftp_user": "pf_ftp", "ftp_password": "pf-ftp-password",
           "s3_endpoint": f"http://127.0.0.1:{ports['s3']}", "pg": pg, "pid": os.getpid()}
    (state / "env.json").write_text(json.dumps(env, indent=2))
    print(json.dumps(env), flush=True)

    def stop(*_):
        subprocess.run(pg["run_as"] + [str(PG_BIN / "pg_ctl"), "-D", pg["data"], "-m", "fast", "stop"], capture_output=True)
        s3.terminate()
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
