"""Structured spec edits that keep comments and order (`factory set | add | remove`, and agent spec patches).

Adapted from pipeline-factory 0.1.0 `pf/edit.py`. Paths use selectors (`rules[r_zip].implementation.params.level`,
`destinations.dev[cro_archive_s3].bucket`); list items are found by rule_id, task_id, id or name. Every edit is
validated; an edit that introduces new errors is rolled back unless keep_invalid is set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from factory_core.envelope import CommandError, Issue
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from factory_core import yamlio

from .files import SPEC_DIR, SPEC_FILES
from .load import ID_KEYS
from .repo import Repo

_TOKEN = re.compile(r"^([A-Za-z_][\w-]*)?((?:\[[^\]]+\])*)$")


@dataclass(frozen=True)
class SpecPatch:
    file: str
    op: Literal["set", "add", "remove"]
    path: str
    value: Any = None


@dataclass
class PatchOutcome:
    issues: list[Issue] = field(default_factory=list)
    rolled_back: bool = False


def parse_value(text: str) -> Any:
    try:
        return yamlio.load_text(text if text.strip() else "null")
    except yamlio.YamlError as exc:
        raise CommandError(f"value is not valid YAML: {exc}", "invalid") from None


def _split(path: str) -> list[str | tuple[str]]:
    """'a.b[x][0].c' -> ['a', 'b', ('x',), ('0',), 'c'] (tuples are selectors)."""
    out: list[str | tuple[str]] = []
    for token in path.split("."):
        m = _TOKEN.match(token)
        if not m or (not m.group(1) and not m.group(2)):
            raise CommandError(f"invalid path segment '{token}'", "invalid")
        if m.group(1):
            out.append(m.group(1))
        out.extend((sel,) for sel in re.findall(r"\[([^\]]+)\]", m.group(2) or ""))
    return out


def _step(node: Any, part: str | tuple[str], create: bool) -> tuple[Any, Any]:
    """Return (container, key) for one path part, creating mappings on demand for `set`."""
    if isinstance(part, tuple):
        sel = part[0]
        if not isinstance(node, list):
            raise CommandError(f"[{sel}] used on something that is not a list", "not_found")
        if sel.isdigit():
            idx = int(sel)
            if idx >= len(node):
                raise CommandError(f"list has no item {idx}", "not_found")
            return node, idx
        for i, it in enumerate(node):
            if isinstance(it, dict) and any(it.get(k) == sel for k in ID_KEYS):
                return node, i
        raise CommandError(f"no item '{sel}' in the list", "not_found")
    if not isinstance(node, dict):
        raise CommandError(f"cannot go into '{part}': not a mapping", "not_found")
    if part not in node:
        if not create:
            raise CommandError(f"no key '{part}'", "not_found")
        node[part] = CommentedMap()
    return node, part


def _locate(doc: Any, path: str, create: bool) -> tuple[Any, Any]:
    parts = _split(path)
    node = doc
    container: Any = None
    key: Any = None
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        if last and not isinstance(p, tuple):
            if not isinstance(node, dict):
                raise CommandError(f"cannot set '{p}': parent is not a mapping", "not_found")
            return node, p
        container, key = _step(node, p, create)
        node = container[key]
    return container, key


def _errors(pipeline_dir: Path, repo: Repo | None) -> tuple[list[Issue], bool]:
    from .validate import validate_dir

    r = validate_dir(pipeline_dir, repo)
    return [i for i in r.issues if i.severity == "error" and i.code != "required"], r.load.spec is not None


def apply_patch(
    pipeline_dir: Path, patch: SpecPatch, *, keep_invalid: bool = False, repo: Repo | None = None
) -> PatchOutcome:
    if patch.file not in SPEC_FILES:
        raise CommandError(f"unknown spec file '{patch.file}' (one of {sorted(SPEC_FILES)})", "invalid")
    f = pipeline_dir / SPEC_DIR / patch.file
    if not f.is_file():
        raise CommandError(f"{SPEC_DIR}/{patch.file} does not exist", "not_found")
    before_text = f.read_text(encoding="utf-8")
    before_errs, crossed_before = _errors(pipeline_dir, repo)
    before = {(i.file, i.loc, i.code, i.msg) for i in before_errs}
    doc = yamlio.load_path(f)
    if patch.op == "set":
        container, key = _locate(doc, patch.path, create=True)
        container[key] = patch.value
    elif patch.op == "add":
        container, key = _locate(doc, patch.path, create=True)
        target = container.get(key) if isinstance(container, dict) else container[key]
        if target is None or (isinstance(target, dict) and not target and isinstance(container, dict)):
            container[key] = target = CommentedSeq()
        if not isinstance(target, list):
            raise CommandError(f"'{patch.path}' is not a list", "invalid")
        target.append(patch.value)
    elif patch.op == "remove":
        container, key = _locate(doc, patch.path, create=False)
        if isinstance(container, dict) and key not in container:
            raise CommandError(f"no key '{key}'", "not_found")
        del container[key]
    else:  # pragma: no cover - Literal guards this
        raise CommandError(f"unknown edit op {patch.op}", "invalid")
    yamlio.dump_path(doc, f)
    after, crossed_after = _errors(pipeline_dir, repo)
    new = [i for i in after if (i.file, i.loc, i.code, i.msg) not in before]
    blame = new
    if crossed_after and not crossed_before:
        # this edit completed the last file, so cross-file checks ran for the first time: findings elsewhere existed
        # before and are reported, not blamed on this edit
        blame = [
            i
            for i in new
            if i.file == patch.file
            and (i.loc == patch.path or i.loc.startswith(patch.path) or patch.path.startswith(i.loc))
        ]
    if blame and not keep_invalid:
        f.write_text(before_text, encoding="utf-8", newline="\n")
        raise CommandError("edit rolled back: it introduced validation errors", "invalid", blame)
    if blame is not new:
        new = [
            i
            if i in blame
            else i.model_copy(update={"severity": "warning", "msg": "already present, now visible: " + i.msg})
            for i in new
        ]
    return PatchOutcome(issues=new, rolled_back=False)
