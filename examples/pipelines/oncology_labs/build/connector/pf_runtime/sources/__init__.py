"""Source adapters. File sources expose list/download; database sources expose batched reads."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RemoteFile:
    path: str  # relative to the source root, '/'-separated, no leading slash
    size: int
    mtime: float  # epoch seconds (UTC)


def join(root: str, rel: str) -> str:
    root = root.rstrip("/") or ""
    return f"{root}/{rel}" if root else f"/{rel}" if not rel.startswith("/") else rel


def open_file_source(source: dict, configuration: dict):
    if source["type"] == "sftp":
        from .sftp import SftpSession

        return SftpSession(source, configuration)
    if source["type"] == "ftp":
        from .ftp import FtpSession

        return FtpSession(source, configuration)
    raise ValueError(f"not a file source: {source['type']}")
