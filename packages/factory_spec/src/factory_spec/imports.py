"""Static checks of custom rule code (rules/<rule_id>/impl.py): an import allow-list and banned names (ADR-0020).

This is a lint that keeps reviewed plugins inside the plugin API; it is NOT a sandbox. In-process Python can always
reach more than it should, so the controls that matter remain the workload's IAM role, the default-deny network policy
and code review. The same check runs in the validator and again when the runtime loads the code.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

ALLOWED_MODULES = frozenset(
    {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "hashlib",
        "itertools",
        "json",
        "math",
        "pydantic",
        "re",
        "typing",
        "factory_runtime.plugin_api",
    }
)
BANNED_NAMES = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "eval",
        "exec",
        "getattr",
        "globals",
        "input",
        "locals",
        "open",
        "setattr",
        "vars",
    }
)


@dataclass(frozen=True)
class ImportFinding:
    line: int
    msg: str


def _module_allowed(module: str) -> bool:
    if module in ALLOWED_MODULES:
        return True
    root = module.split(".", 1)[0]
    return root in ALLOWED_MODULES and root != "factory_runtime"


def check_plugin_source(source: str) -> list[ImportFinding]:
    """Findings for imports outside the allow-list, banned built-ins and any dunder access."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [ImportFinding(exc.lineno or 0, "the code does not parse")]
    out: list[ImportFinding] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            out += [
                ImportFinding(line, f"import of '{a.name}' is not allowed")
                for a in node.names
                if not _module_allowed(a.name)
            ]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                out.append(ImportFinding(line, "relative imports are not allowed"))
            elif not _module_allowed(node.module or ""):
                out.append(ImportFinding(line, f"import from '{node.module}' is not allowed"))
        elif isinstance(node, ast.Name) and (node.id in BANNED_NAMES or node.id.startswith("__")):
            out.append(ImportFinding(line, f"use of '{node.id}' is not allowed"))
        elif isinstance(node, ast.Attribute) and "__" in node.attr:
            out.append(ImportFinding(line, f"access to '{node.attr}' is not allowed"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            out.append(ImportFinding(line, "strings containing '__' are not allowed"))
    return sorted(set(out), key=lambda f: (f.line, f.msg))
