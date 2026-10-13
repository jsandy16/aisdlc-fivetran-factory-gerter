"""Schema migrations: pure, chained functions over the round-trip YAML documents of a spec (comments are kept).

The registry is empty for schema 1.0.0; tests register a dummy 1.0.0 -> 1.1.0 migration to prove the machinery.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory_core.envelope import CommandError

from factory_core import yamlio

from ..files import SPEC_DIR, SPEC_FILES

CURRENT_SCHEMA_VERSION = "1.0.0"
Docs = dict[str, Any]


@dataclass(frozen=True)
class Migration:
    from_version: str
    to_version: str
    description: str
    apply: Callable[[Docs], None]


@dataclass
class Registry:
    migrations: dict[str, Migration] = field(default_factory=dict)

    def register(self, m: Migration) -> Migration:
        if m.from_version in self.migrations:
            raise ValueError(f"a migration from {m.from_version} is already registered")
        self.migrations[m.from_version] = m
        return m

    def chain(self, from_version: str, to_version: str) -> list[Migration]:
        out: list[Migration] = []
        v = from_version
        while v != to_version:
            m = self.migrations.get(v)
            if m is None:
                raise CommandError(f"no migration path from schema {from_version} to {to_version}", "migration")
            out.append(m)
            v = m.to_version
        return out

    @property
    def latest(self) -> str:
        v = CURRENT_SCHEMA_VERSION
        while v in self.migrations:
            v = self.migrations[v].to_version
        return v


DEFAULT_REGISTRY = Registry()


@dataclass
class MigrationReport:
    from_version: str
    to_version: str
    applied: list[str]
    changed_files: list[str]


def migrate_dir(
    pipeline_dir: Path, *, to: str | None = None, registry: Registry = DEFAULT_REGISTRY, dry_run: bool = False
) -> MigrationReport:
    spec_dir = pipeline_dir / SPEC_DIR
    docs: Docs = {}
    before: dict[str, str] = {}
    for fname in SPEC_FILES:
        f = spec_dir / fname
        if f.is_file():
            before[fname] = f.read_text(encoding="utf-8")
            docs[fname] = yamlio.load_text(before[fname])
    pipe = docs.get("pipeline.yaml")
    if pipe is None:
        raise CommandError("pipeline.yaml is missing", "not_found")
    current = str(pipe.get("schema_version"))
    target = to or registry.latest
    chain = registry.chain(current, target)
    for m in chain:
        m.apply(docs)
        docs["pipeline.yaml"]["schema_version"] = m.to_version
    changed = []
    for fname, doc in docs.items():
        text = yamlio.dump_text(doc)
        if text != before[fname]:
            changed.append(fname)
            if not dry_run:
                (spec_dir / fname).write_text(text, encoding="utf-8", newline="\n")
    return MigrationReport(
        current, target, [f"{m.from_version} -> {m.to_version}: {m.description}" for m in chain], sorted(changed)
    )
