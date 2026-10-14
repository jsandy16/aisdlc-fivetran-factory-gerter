"""Change request M1: secrets per environment, and a developer's local secrets file for local runs only."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from factory_core.errors import ConfigError, SecurityError
from factory_runtime.envcheck import check_environment
from factory_runtime.secrets.local_file import LocalFileProvider
from factory_runtime.secrets.provider import ResolvedRef
from factory_runtime.secrets.router import build_secrets
from factory_runtime.services import Services
from factory_spec.load import load_spec
from factory_spec.secrets import effective_provider
from rt_helpers import EXAMPLE_REPO, LogCapture, copy_repo, make_config

DOC = """\
clinops/dev/portal-client: {client_id: synthetic-id, client_secret: synthetic-local-secret-0001}
clinops/dev/partner-sftp: {private_key: synthetic-local-key-0001}
clinops/dev/cro-upload: {token: ""}
plain/text: synthetic-plain-0001
"""


def _file(tmp_path: Path, mode: int = 0o600, text: str = DOC) -> Path:
    f = tmp_path / "dev.yaml"
    f.write_text(text)
    os.chmod(f, mode)
    return f


def _ref(path: str, key: str | None) -> ResolvedRef:
    return ResolvedRef("s", "local_file", path, key, None, "generic")


def test_local_file_provider(tmp_path: Path) -> None:
    p = LocalFileProvider(_file(tmp_path))
    with p.get(_ref("clinops/dev/portal-client", "client_secret"), purpose="t").reveal() as v:
        assert v == "synthetic-local-secret-0001"
    with p.get(_ref("plain/text", None), purpose="t").reveal() as v:
        assert v == "synthetic-plain-0001"
    with p.get(_ref("clinops/dev/portal-client", None), purpose="t").reveal() as v:
        assert '"client_id": "synthetic-id"' in v  # a whole multi-field secret is its JSON document
    for ref, code in [(_ref("clinops/dev/cro-upload", "token"), "secret_key_missing"),
                      (_ref("nowhere", None), "secret_missing"),
                      (_ref("plain/text", "k"), "secret_key_missing")]:  # fmt: skip
        with pytest.raises(ConfigError) as e:
            p.get(ref, purpose="t")
        assert e.value.code == code


def test_local_file_must_be_private_and_present(tmp_path: Path) -> None:
    with pytest.raises(SecurityError) as e:
        LocalFileProvider(_file(tmp_path, 0o644)).get(_ref("plain/text", None), purpose="t")
    assert e.value.code == "local_secrets_file_permissions"
    with pytest.raises(ConfigError) as c:
        LocalFileProvider(tmp_path / "missing.yaml").get(_ref("plain/text", None), purpose="t")
    assert c.value.code == "local_secrets_file_missing"


def test_environment_decides_the_store(tmp_path: Path) -> None:
    repo = copy_repo(EXAMPLE_REPO, tmp_path / "repo")
    spec = load_spec(repo / "pipelines" / "study_pkg_delivery").spec
    assert spec is not None
    assert effective_provider(spec, "prod", "partner_sftp_key") == "vault"  # declared with the secret (older specs)
    envs = spec.environments.environments
    stores = envs["prod"].secrets.model_copy(update={"default": "aws_secrets_manager",
                                                     "overrides": {"portal_client": "vault"}})  # fmt: skip
    envs["prod"] = envs["prod"].model_copy(update={"secrets": stores})
    assert effective_provider(spec, "prod", "partner_sftp_key") == "aws_secrets_manager"
    assert effective_provider(spec, "prod", "portal_client") == "vault"
    assert effective_provider(spec, "dev", "partner_sftp_key") == "vault"


def test_local_run_reads_every_secret_from_the_file(tmp_path: Path, workdir: Path, aws: None) -> None:
    repo = copy_repo(EXAMPLE_REPO, tmp_path / "repo")
    spec = load_spec(repo / "pipelines" / "study_pkg_delivery").spec
    cfg = make_config(workdir, "study_pkg_delivery", local_secrets_file=_file(tmp_path))
    events: list[tuple[str, dict[str, object]]] = []
    with LogCapture() as logs:
        secrets = build_secrets(Services.build(cfg), spec, "dev", lambda e, **kw: events.append((e, kw)))
        with secrets.get("partner_sftp_key", purpose="sftp").reveal() as key:  # declared in Vault
            assert key == "synthetic-local-key-0001"
        logs_text = logs.text
    assert events[0][1]["provider"] == "local_file"
    assert "synthetic-local-key-0001" not in logs_text
    prod = make_config(workdir, "study_pkg_delivery", env="prod", local_secrets_file=_file(tmp_path))
    with pytest.raises(SecurityError) as e:
        build_secrets(Services.build(prod), spec, "prod", lambda *a, **k: None)
    assert e.value.code == "local_secrets_forbidden"
    with pytest.raises(SecurityError):
        check_environment("prod", {"FACTORY_LOCAL_SECRETS_FILE": "/home/u/.factory/secrets/x.yaml"})
