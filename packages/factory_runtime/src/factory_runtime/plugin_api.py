"""The plugin API: everything a custom rule (rules/<rule_id>/impl.py) may import from the runtime (ADR-0020).

A custom rule is one class named `Rule`:

    from factory_runtime.plugin_api import CustomRule, PluginContext, PluginInputs, PluginOutputs, PluginParams
    from factory_runtime.plugin_api import ObjectListing

    class Params(PluginParams): ...
    class Inputs(PluginInputs):
        listing: ObjectListing            # one field per input artifact, named like the artifact
    class Outputs(PluginOutputs):
        study_count: int                  # one field per output artifact

    class Rule(CustomRule[Params, Inputs, Outputs]):
        side_effects = frozenset()        # what the rule may do through the context (SIDE_EFFECTS)
        def run(self, ctx: PluginContext, params: Params, inputs: Inputs) -> Outputs: ...

What the context offers: the pinned clock and run date, a hashed run key, a log, files in the rule's own workdir,
and reading a staged package. Custom rules get no secrets (decision P3-7) and no AWS or network clients. The import
allow-list is a lint that keeps reviewed code on this API; it is not a sandbox: IAM, the network policy and review
are the real controls.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import IO, Any, ClassVar, Generic, TypeVar

from factory_spec.artifact_models import (
    ChecksumRecord,
    DestinationSet,
    Manifest,
    ManifestFile,
    ObjectEntry,
    ObjectListing,
    PackageInfo,
    PackageMember,
    PortalSnapshot,
    Receipt,
    TriggerSnapshot,
)
from pydantic import BaseModel, ConfigDict

SIDE_EFFECTS = frozenset({"reads_staging", "reads_workdir", "writes_workdir"})
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

__all__ = [
    "SIDE_EFFECTS",
    "ChecksumRecord",
    "CustomRule",
    "DestinationSet",
    "Manifest",
    "ManifestFile",
    "ObjectEntry",
    "ObjectListing",
    "PackageInfo",
    "PackageMember",
    "PluginContext",
    "PluginInputs",
    "PluginOutputs",
    "PluginParams",
    "PortalSnapshot",
    "Receipt",
    "RuleFailed",
    "TriggerSnapshot",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=False)


class PluginParams(_Strict):
    """The rule's parameters (none in v1: rules.yaml gives custom rules no params)."""


class PluginInputs(_Strict):
    """One field per input artifact of the rule, named like the artifact."""


class PluginOutputs(_Strict):
    """One field per output artifact of the rule, named like the artifact."""


class RuleFailed(Exception):  # noqa: N818 - reads naturally in plugin code: raise RuleFailed("...")
    """Raise when the rule's check does not hold; the rule's on_failure then decides (fail, warn, quarantine)."""


class PluginContext:
    """The only door from a custom rule to the run. Every call is recorded with the side effect it needs."""

    def __init__(self, *, run_date: str, run_key_hash: str, now: int, workdir: Path, log: Any, opener: Any) -> None:
        self.run_date = run_date
        self.run_key_hash = run_key_hash
        self._now = now
        self._workdir = workdir
        self._log = log
        self._opener = opener
        self.used: set[str] = set()

    def now(self) -> int:
        """The run's pinned clock (seconds since the epoch): the same in every task and retry."""
        return self._now

    def log(self, event: str, **fields: str | int | bool | None) -> None:
        self._log(f"plugin_{event}", **fields)

    def _file(self, name: str) -> Path:
        if not _FILE_NAME.match(name):
            raise ValueError("workdir files are single safe names (letters, digits, '.', '_', '-')")
        return self._workdir / name

    def write_file(self, name: str, data: bytes) -> None:
        self.used.add("writes_workdir")
        self._file(name).write_bytes(data)

    def read_file(self, name: str) -> bytes:
        self.used.add("reads_workdir")
        return self._file(name).read_bytes()

    def open_package(self, package: PackageInfo) -> IO[bytes]:
        """A read-only stream of a staged package of this run (size checked at the end)."""
        self.used.add("reads_staging")
        stream: IO[bytes] = self._opener(package)
        return stream


P = TypeVar("P", bound=PluginParams)
I = TypeVar("I", bound=PluginInputs)  # noqa: E741 - the documented contract names
O = TypeVar("O", bound=PluginOutputs)  # noqa: E741


class CustomRule(Generic[P, I, O]):
    side_effects: ClassVar[frozenset[str]] = frozenset()
    aws_actions: ClassVar[tuple[str, ...]] = ()

    def run(self, ctx: PluginContext, params: P, inputs: I) -> O:  # pragma: no cover - implemented by the plugin
        raise NotImplementedError
