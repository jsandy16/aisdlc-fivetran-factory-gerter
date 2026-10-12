"""Structured edits to spec files that keep comments and order (`pf set`, `pf add`, `pf remove`, `pf add-check`).

People can edit YAML directly too; these commands exist so agents and scripts never patch YAML as text.
Every edit is validated; an edit that makes the bundle less valid is rolled back unless --keep-invalid.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from . import yamlio
from .bundle import SPEC_FILES, load_bundle
from .envelope import Issue, PfError


def _parse_value(text: str) -> Any:
    try:
        return yamlio.loads(text)
    except Exception as exc:
        raise PfError(f"value is not valid YAML: {exc}", "invalid") from None


def _walk(node: Any, parts: list[str], create: bool) -> tuple[Any, str]:
    for p in parts[:-1]:
        if isinstance(node, list):
            if not p.isdigit() or int(p) >= len(node):
                raise PfError(f"no list item '{p}'", "not_found")
            node = node[int(p)]
        elif isinstance(node, dict):
            if p not in node:
                if not create:
                    raise PfError(f"no key '{p}'", "not_found")
                node[p] = CommentedMap()
            node = node[p]
        else:
            raise PfError(f"cannot descend into a scalar at '{p}'", "invalid")
    return node, parts[-1]


def _resolve_selectors(doc: Any, path: str) -> list[str]:
    """Allow name selectors: entities[lab_results].attributes[visit_date].checks -> numeric indexes."""
    import re

    parts: list[str] = []
    node = doc
    for token in path.split("."):
        m = re.fullmatch(r"([A-Za-z_][\w-]*)\[([^\]]+)\]", token)
        if m:
            key, sel = m.group(1), m.group(2)
            parts.append(key)
            node = node[key] if isinstance(node, dict) and key in node else None
            if not isinstance(node, list):
                raise PfError(f"'{key}' is not a list", "not_found")
            idx = next((i for i, it in enumerate(node) if isinstance(it, dict) and (it.get("name") == sel or it.get("id") == sel)), None)
            if idx is None:
                raise PfError(f"no item named '{sel}' in {key}", "not_found")
            parts.append(str(idx))
            node = node[idx]
        else:
            parts.append(token)
            if isinstance(node, dict):
                node = node.get(token)
            elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
                node = node[int(token)]
            else:
                node = None
    return parts


def _errors(path: Path) -> list[Issue]:
    return [i for i in load_bundle(path).issues if i.severity == "error" and i.code != "required"]


def apply_edit(pipeline_path: Path, file: str, path: str, op: str, value_text: str | None = None, keep_invalid: bool = False) -> list[Issue]:
    if file not in SPEC_FILES:
        raise PfError(f"unknown spec file '{file}' (one of {sorted(SPEC_FILES)})", "invalid")
    f = pipeline_path / file
    before_text = f.read_text(encoding="utf-8")
    before = {(i.file, i.loc, i.msg) for i in _errors(pipeline_path)}
    doc = yamlio.load(f)
    parts = _resolve_selectors(doc, path)
    parent, last = _walk(doc, parts, create=(op == "set"))
    if op == "set":
        val = _parse_value(value_text or "")
        if isinstance(parent, list):
            parent[int(last)] = val
        else:
            parent[last] = val
    elif op == "add":
        val = _parse_value(value_text or "")
        target = parent[int(last)] if isinstance(parent, list) else parent.get(last)
        if target is None and isinstance(parent, dict):
            parent[last] = target = CommentedSeq()
        if not isinstance(target, list):
            raise PfError(f"'{path}' is not a list", "invalid")
        target.append(val)
    elif op == "remove":
        if isinstance(parent, list):
            del parent[int(last)]
        elif last in parent:
            del parent[last]
        else:
            raise PfError(f"no key '{last}'", "not_found")
    else:
        raise PfError(f"unknown edit op {op}", "invalid")
    yamlio.dump(doc, f)
    after = _errors(pipeline_path)
    new = [i for i in after if (i.file, i.loc, i.msg) not in before]
    if new and not keep_invalid:
        f.write_text(before_text, encoding="utf-8")
        raise PfError("edit rolled back: it introduced validation errors", "invalid", new)
    return new


def check_path(entity: str | None, attribute: str | None, package: bool, row: bool) -> tuple[str, str]:
    if package:
        return "source.yaml", "source.package.checks"
    if not entity:
        raise PfError("--entity is required (or --package)", "invalid")
    if attribute:
        return "sttm.yaml", f"entities[{entity}].attributes[{attribute}].checks"
    if row:
        return "sttm.yaml", f"entities[{entity}].row_checks"
    return "sttm.yaml", f"entities[{entity}].checks"
