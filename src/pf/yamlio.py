"""YAML read/write that keeps comments and key order (round-trip), so people and tools can both edit specs."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


def _yaml() -> YAML:
    y = YAML()  # round-trip
    y.preserve_quotes = True
    y.width = 120
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return _yaml().load(fh)


def loads(text: str) -> Any:
    return _yaml().load(text)


def dump(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    _yaml().dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8", newline="\n")


def dumps(data: Any) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def to_plain(node: Any) -> Any:
    """Commented structures -> plain dict/list (for pydantic)."""
    if isinstance(node, (CommentedMap, dict)):
        return {str(k): to_plain(v) for k, v in node.items()}
    if isinstance(node, (CommentedSeq, list)):
        return [to_plain(v) for v in node]
    return node
