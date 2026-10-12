"""Amazon RDS (PostgreSQL / MySQL) reads: server-side cursors, cursor-column increments, read-only sessions."""
from __future__ import annotations

from typing import Any, Iterator

from ..errors import ConfigError, TransientError
from .tlsx import ca_file, verifying_context


def _quote(engine: str, ident: str) -> str:
    q = '"' if engine == "postgres" else "`"
    return ".".join(q + part.replace(q, q + q) + q for part in ident.split("."))


class RdsSession:
    def __init__(self, source: dict, configuration: dict):
        self.engine = source["engine"]
        pw = configuration.get(source["password"]["secret"])
        if not pw:
            raise ConfigError(f"configuration key '{source['password']['secret']}' is missing or empty")
        timeout = int(source.get("timeout_seconds", 60))
        try:
            mode = source.get("ssl_mode", "require")
            if self.engine == "postgres":
                import psycopg2

                kw: dict[str, Any] = {"sslmode": mode}
                if mode == "verify-full":
                    # RDS certificates chain to the AWS RDS CA, which is not in public trust stores
                    kw["sslrootcert"] = ca_file(source, configuration) or "system"
                self.conn = psycopg2.connect(
                    host=source["host"], port=source.get("port") or 5432, dbname=source["database"],
                    user=source["username"], password=pw, connect_timeout=timeout, application_name="pf_runtime", **kw,
                )
                self.conn.set_session(readonly=True, autocommit=False)
            else:
                import ssl

                import pymysql

                ssl_arg: Any = None
                if mode == "verify-full":
                    ssl_arg = verifying_context(source, configuration)
                elif mode == "require":  # encrypted, server identity not verified (allowed outside GxP only)
                    ssl_arg = ssl.create_default_context()
                    ssl_arg.check_hostname = False
                    ssl_arg.verify_mode = ssl.CERT_NONE
                self.conn = pymysql.connect(
                    host=source["host"], port=source.get("port") or 3306, database=source["database"],
                    user=source["username"], password=pw, connect_timeout=timeout, read_timeout=timeout * 10,
                    ssl=ssl_arg, cursorclass=pymysql.cursors.SSCursor, autocommit=False,
                )
                with self.conn.cursor() as cur:  # read-only like the postgres session
                    cur.execute("SET SESSION TRANSACTION READ ONLY")
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            msg = str(exc).lower()
            if isinstance(exc, ConfigError):
                raise
            if "password" in msg or "authentication" in msg or "access denied" in msg or "certificate" in msg or "ssl" in msg:
                raise ConfigError(f"database authentication failed ({name})") from None
            raise TransientError(f"database connect failed ({name})") from None

    def columns(self, table: str) -> set[str]:
        schema, _, name = table.rpartition(".")
        cur = self.conn.cursor()
        try:
            if self.engine == "postgres":
                cur.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
                    (schema or "public", name),
                )
            else:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = COALESCE(%s, DATABASE()) AND table_name = %s",
                    (schema or None, name),
                )
            return {r[0] for r in cur.fetchall()}
        finally:
            cur.close()

    def read(self, table: str, columns: list[str], cursor_column: str | None, since: Any, order_by: list[str], batch_size: int) -> Iterator[list[dict]]:
        cols = ", ".join(_quote(self.engine, c) for c in columns)
        sql = f"SELECT {cols} FROM {_quote(self.engine, table)}"
        params: tuple = ()
        if cursor_column:
            if since is not None:
                # >= with an idempotent upsert: rows sharing the boundary value are never skipped
                sql += f" WHERE {_quote(self.engine, cursor_column)} >= %s"
                params = (since,)
            sql += " ORDER BY " + ", ".join(_quote(self.engine, c) for c in [cursor_column, *order_by])
        if self.engine == "postgres":
            cur = self.conn.cursor(name="pf_read")  # server-side cursor
            cur.itersize = batch_size
        else:
            cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                yield [dict(zip(columns, r)) for r in rows]
        finally:
            cur.close()
            self.conn.rollback()

    def close(self) -> None:
        self.conn.close()
