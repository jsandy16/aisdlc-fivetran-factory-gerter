"""Cross-file validation of a loaded PipelineSpec. Every finding is an Issue with a registered code and a hint."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from factory_core.envelope import Issue

from ..load import LoadResult, load_spec
from ..repo import Repo, load_repo
from .checks import CHECKS, Context


@dataclass
class ValidationResult:
    load: LoadResult
    issues: list[Issue]

    @property
    def ok(self) -> bool:
        return self.load.spec is not None and not any(i.severity == "error" for i in self.issues)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]


def validate_spec(load: LoadResult, repo: Repo) -> list[Issue]:
    if load.spec is None:
        return []
    ctx = Context(spec=load.spec, repo=repo)
    out: list[Issue] = []
    for check in CHECKS:
        out.extend(check(ctx))
    return out


def validate_dir(pipeline_dir: Path, repo: Repo | None = None) -> ValidationResult:
    load = load_spec(pipeline_dir)
    repo = repo or load_repo(pipeline_dir)
    issues = list(load.issues) + validate_spec(load, repo)
    # stable order: file, location, code
    issues.sort(key=lambda i: (i.file, i.loc, i.code, i.msg))
    return ValidationResult(load, issues)


__all__ = ["ValidationResult", "validate_dir", "validate_spec"]
