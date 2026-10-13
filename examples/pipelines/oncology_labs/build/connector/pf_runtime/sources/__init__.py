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
    """Adapters are imported by name: a connector vendors only its own adapter, and the Connector SDK's pipreqs
    scan must not see the others as top-level imports (it would look `sftp`/`ftp` up on PyPI)."""
    import importlib

    classes = {"sftp": "SftpSession", "ftp": "FtpSession"}
    if source["type"] not in classes:
        raise ValueError(f"not a file source: {source['type']}")
    module = importlib.import_module(f"{__name__}.{source['type']}")
    return getattr(module, classes[source["type"]])(source, configuration)
