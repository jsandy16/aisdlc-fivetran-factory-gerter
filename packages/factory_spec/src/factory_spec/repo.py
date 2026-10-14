"""The team pipelines repository: `factory.toml` marker, environments, team and platform settings location."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from factory_core.envelope import CommandError
from pydantic import ValidationError

from .catalog import Pricing, pricing
from .common import StrictModel, ba
from .fieldtypes import EnvName, Identifier
from .platform import PlatformSettings

MARKER = "factory.toml"
PRICING_FILE = "pricing.json"
DEFAULT_ENVIRONMENTS = ("dev", "test", "ppt", "prod")


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
