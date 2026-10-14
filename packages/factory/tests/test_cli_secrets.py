"""Change request M1: `factory secrets template` writes names and keys only, private, merged, dev only."""

from __future__ import annotations

import io
import json
import stat
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from cli_helpers import copy_example
from factory.cli import main

from factory_core import yamlio


def _run(repo: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> dict[str, Any]:
    monkeypatch.chdir(repo)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["--json", *args])
    out: dict[str, Any] = json.loads(buf.getvalue())
    out["_code"] = code
    return out


def test_secrets_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = copy_example(tmp_path / "repo")
    f = tmp_path / "home" / "dev.yaml"
    first = _run(repo, monkeypatch, "secrets", "template", "study_pkg_delivery", "--out", str(f))
    assert first["_code"] == 0
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    doc = yamlio.to_plain(yamlio.load_path(f))
    assert doc["clinops/dev/partner-sftp"] == {"private_key": ""}
    assert doc["clinops/dev/cro-upload"] == {"token": ""}
    assert first["data"]["still_empty"] == 3
    doc["clinops/dev/cro-upload"]["token"] = "synthetic-filled-0001"
    f.write_text(yamlio.dump_text(doc))
    again = _run(repo, monkeypatch, "secrets", "template", "study_pkg_delivery", "--out", str(f))
    assert again["data"]["added"] == []
    assert yamlio.to_plain(yamlio.load_path(f))["clinops/dev/cro-upload"]["token"] == "synthetic-filled-0001"
    assert "synthetic-filled-0001" not in json.dumps(again)  # values are never echoed
    prod = _run(repo, monkeypatch, "secrets", "template", "study_pkg_delivery", "--env", "prod", "--out", str(f))
    assert prod["_code"] != 0
