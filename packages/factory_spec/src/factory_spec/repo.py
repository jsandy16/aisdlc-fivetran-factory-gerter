"""The team pipelines repository: `factory.toml` marker, environments, team and platform settings location."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from factory_core.envelope import CommandError
from pydantic import ValidationError, model_validator

from .catalog import Pricing, pricing
from .common import StrictModel, ba
from .fieldtypes import EnvName, Identifier, S3Bucket
from .platform import PlatformSettings

MARKER = "factory.toml"
PRICING_FILE = "pricing.json"
DEFAULT_ENVIRONMENTS = ("dev", "test", "ppt", "prod")


class DataopsRepo(StrictModel):
    """The DataOps layout (ADR-0026): what the generated repository, image and DAG files have in common."""

    project: str = ba("Project", "Project name in the DataOps platform, e.g. rd-dpl-studydata",
                      pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")  # fmt: skip
    dist_bucket: S3Bucket = ba("Deployment bucket", "Bucket the containers sync src/ from (deployments/<branch>)")
    base_image: str = ba("Base image", "FROM image of docker/Dockerfile", pattern=r"^[A-Za-z0-9./:@_-]{3,255}$")
    python: str = ba("Python", "Interpreter in the image", default="python3.13", pattern=r"^python3\.\d{1,2}$")
    uv_version: str = ba("uv version", "Installed in the image", default="0.11.26", pattern=r"^\d+\.\d+\.\d+$")
    dag_version: str = ba("DAG format version", "The DataOps DAG JSON version", default="v3",
                          pattern=r"^v\d{1,3}$")  # fmt: skip
    env_var: str = ba("Environment variable", "Names the environment inside the container", default="project_env",
                      pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")  # fmt: skip
    ca_bundle: str | None = ba("CA bundle", "Trusted CA file inside the image (corporate CA), trusted for HTTPS",
                               default=None, pattern=r"^/[A-Za-z0-9._/-]{1,250}$")  # fmt: skip
    proxy: str | None = ba("Proxy", "HTTP(S) proxy the image sets", default=None,
                           pattern=r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$")  # fmt: skip
    no_proxy: str | None = ba("No proxy", "Hosts that bypass the proxy", default=None,
                              pattern=r"^[A-Za-z0-9.,*-]{1,1000}$")  # fmt: skip


class RepoConfig(StrictModel):
    api_version: str = ba("API version", "", default="factory/v1", pattern=r"^factory/v1$")
    team: Identifier | None = ba("Team", "The team owning this repository", default=None)
    environments: list[EnvName] = ba(
        "Environments",
        "Environments every pipeline must bind",
        default_factory=lambda: list(DEFAULT_ENVIRONMENTS),
        min_length=1,
    )
    platform_settings: str | None = ba("Platform settings", "Path to the platform settings folder", default=None)
    layout: Literal["mwaa", "dataops"] = ba(
        "Layout", "mwaa: Python DAGs, Lambda and pods; dataops: DAG JSON and containers (ADR-0026)", default="mwaa"
    )
    dataops: DataopsRepo | None = ba("DataOps", "Settings of the dataops layout", default=None)

    @model_validator(mode="after")
    def _layout(self) -> RepoConfig:
        if self.layout == "dataops" and self.dataops is None:
            raise ValueError('layout = "dataops" needs a [dataops] table')
        return self


@dataclass
class Repo:
    root: Path | None
    config: RepoConfig
    platform: dict[str, PlatformSettings] = field(default_factory=dict)
    platform_issues: list[str] = field(default_factory=list)
    calibrated: Pricing | None = None  # the platform team's calibrated prices; never mixed with the placeholders

    @property
    def prices(self) -> Pricing:
        return self.calibrated or pricing()

    @property
    def environments(self) -> list[str]:
        return list(self.config.environments)


def find_repo_root(start: Path | None = None) -> Path | None:
    here = (start or Path.cwd()).resolve()
    for p in (here, *here.parents):
        if (p / MARKER).is_file():
            return p
    return None


def load_repo(start: Path | None = None) -> Repo:
    root = find_repo_root(start)
    if root is None:
        return Repo(None, RepoConfig())
    try:
        cfg = RepoConfig.model_validate(tomllib.loads((root / MARKER).read_text(encoding="utf-8")))
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise CommandError(f"{MARKER} is invalid: {exc}", "config") from None
    repo = Repo(root, cfg)
    if cfg.platform_settings:
        d = (root / cfg.platform_settings).resolve()
        for env in cfg.environments:
            f = d / f"{env}.json"
            if not f.is_file():
                repo.platform_issues.append(f"platform settings for '{env}' not found at {f.name}")
                continue
            try:
                repo.platform[env] = PlatformSettings.model_validate(json.loads(f.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, ValidationError) as exc:
                repo.platform_issues.append(f"platform settings {f.name} are invalid: {str(exc).splitlines()[0]}")
        prices = d / PRICING_FILE
        if prices.is_file():
            try:
                repo.calibrated = Pricing.model_validate(json.loads(prices.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise CommandError(f"{PRICING_FILE} is invalid: {str(exc).splitlines()[0]}", "config") from None
            if repo.calibrated.status != "calibrated":
                raise CommandError(f"{PRICING_FILE} must say status: calibrated (placeholders live in the catalog)",
                                   "config")  # fmt: skip
    return repo
