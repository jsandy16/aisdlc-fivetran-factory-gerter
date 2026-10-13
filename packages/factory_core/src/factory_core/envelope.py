"""The one result shape every `factory --json` command prints (agents, hooks and CI parse it).

Adapted from pipeline-factory 0.1.0 `pf/envelope.py`; adds `hint` (a plain-English fix suggestion).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import __version__

Severity = Literal["error", "warning"]


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: Severity = "error"
    file: str = ""
    loc: str = Field(default="", description="Selector path inside the file, e.g. rules[r_zip].implementation.params")
    code: str = Field(description="Stable machine code, e.g. required, unresolved_reference")
    msg: str
    hint: str | None = Field(default=None, description="Plain-English suggestion how to fix it")


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: Literal["factory/v1"] = "factory/v1"
    command: str
    ok: bool
    factory_version: str = __version__
    issues: list[Issue] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]


class CommandError(Exception):
    """A user-facing build-time failure with a stable code; the CLI turns it into a Result."""

    def __init__(self, msg: str, code: str = "error", issues: list[Issue] | None = None) -> None:
        super().__init__(msg)
        self.code = code
        self.issues = issues or []
