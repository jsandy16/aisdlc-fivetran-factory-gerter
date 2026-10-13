from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from moto import mock_aws


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """In-process moto with synthetic credentials; nothing leaves the process."""
    for k in ("AWS_PROFILE", "AWS_ENDPOINT_URL", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    with mock_aws():
        yield


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d
