"""Round-trip YAML for specs (adapted from pipeline-factory 0.1.0 `pf/yamlio.py`).

Comments and key order are kept so people and agents can both edit specs. Constructs that hide or change meaning are
rejected with friendly codes instead of being loaded: aliases (unbounded expansion, values hidden from review), tags,
duplicate keys, unquoted dates and non-string keys.
"""

from __future__ import annotations

import datetime as dt
import io
import os
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import MarkedYAMLError, YAMLError

MAX_BYTES = 1 << 20


class YamlError(ValueError):
    """A YAML problem with a stable code: yaml_syntax, yaml_alias, yaml_tag, yaml_duplicate_key, yaml_date,
    yaml_key_type, yaml_too_large."""

    def __init__(self, code: str, message: str, *, line: int | None = None, loc: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.line = line
        self.loc = loc


def _yaml() -> YAML:
    y = YAML()  # round-trip
    y.preserve_quotes = True
    y.width = 4096  # never re-wrap long scalars: a one-line edit stays a one-line diff
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _scan(text: str) -> None:
    """Reject aliases and explicit tags before constructing anything."""
    from ruamel.yaml.events import AliasEvent, CollectionStartEvent, ScalarEvent

    try:
        for ev in YAML(typ="safe", pure=True).parse(text):
            line = ev.start_mark.line + 1 if ev.start_mark is not None else None
            if isinstance(ev, AliasEvent):
                raise YamlError("yaml_alias", "YAML aliases (*name) are not allowed in spec files", line=line)
            if isinstance(ev, ScalarEvent | CollectionStartEvent) and ev.tag is not None:
                if isinstance(ev, ScalarEvent) and ev.implicit and ev.implicit[0]:
                    continue
                raise YamlError("yaml_tag", f"YAML tags ({ev.tag}) are not allowed in spec files", line=line)
    except MarkedYAMLError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark is not None else None
        raise YamlError("yaml_syntax", f"YAML syntax error: {exc.problem}", line=line) from None
    except YAMLError as exc:  # pragma: no cover - other parser errors
        raise YamlError("yaml_syntax", f"YAML syntax error: {exc}") from None


def load_text(text: str, *, max_bytes: int = MAX_BYTES) -> Any:
    if len(text.encode("utf-8")) > max_bytes:
        raise YamlError("yaml_too_large", f"spec file is larger than {max_bytes} bytes")
    _scan(text)
    try:
        return _yaml().load(text)
    except DuplicateKeyError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark is not None else None
        raise YamlError("yaml_duplicate_key", "a key appears twice in the same mapping", line=line) from None
    except MarkedYAMLError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark is not None else None
        raise YamlError("yaml_syntax", f"YAML syntax error: {exc.problem}", line=line) from None


def load_path(path: Path) -> Any:
    return load_text(Path(path).read_text(encoding="utf-8"))


def dump_text(doc: Any) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def dump_path(doc: Any, path: Path) -> None:
    """Write atomically (temp file + replace), LF line endings."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dump_text(doc)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def to_plain(node: Any, loc: str = "") -> Any:
    """Round-trip structures -> plain dict/list/str/int/float/bool/None (for pydantic)."""
    if isinstance(node, TaggedScalar):
        raise YamlError("yaml_tag", "YAML tags are not allowed in spec files", loc=loc)
    if isinstance(node, CommentedMap | dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if not isinstance(k, str):
                raise YamlError("yaml_key_type", f"key {k!r} must be text; quote it", loc=loc)
            out[str(k)] = to_plain(v, f"{loc}.{k}" if loc else str(k))
        return out
    if isinstance(node, CommentedSeq | list):
        return [to_plain(v, f"{loc}.{i}") for i, v in enumerate(node)]
    if isinstance(node, dt.date | dt.datetime):
        raise YamlError("yaml_date", 'dates must be quoted text (e.g. "2024-03-01")', loc=loc)
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if isinstance(node, str):
        return str(node)
    raise YamlError("yaml_tag", f"unsupported YAML value of type {type(node).__name__}", loc=loc)
