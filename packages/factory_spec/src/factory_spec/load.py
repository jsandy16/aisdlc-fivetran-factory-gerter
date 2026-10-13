"""Load a spec folder: YAML problems, <REQUIRED> placeholders and model errors become Issues with selector paths.

Selector paths name list items by their id, e.g. `rules[r_zip].implementation.params.level`, and leave out the
discriminator tags pydantic inserts (category, kind, op, type, via), so a person or an agent can find the field and
paste the path into `factory set`. Cross-file validation runs only when all six files parse.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory_core.envelope import Issue
from pydantic import ValidationError

from factory_core import yamlio

from .codes import issue
from .common import REQUIRED
from .files import SPEC_DIR, SPEC_FILES
from .spec import PipelineSpec

TAG_KEYS = ("category", "kind", "op", "type", "via")
ID_KEYS = ("rule_id", "task_id", "id", "name")
SUPPORTED_SCHEMA = (1, 0)


@dataclass
class LoadResult:
    path: Path
    spec: PipelineSpec | None
    issues: list[Issue] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.spec is not None and not any(i.severity == "error" for i in self.issues)


def _item_key(node: Any, index: int) -> str:
    if isinstance(node, dict):
        for k in ID_KEYS:
            v = node.get(k)
            if isinstance(v, str) and v and v != REQUIRED:
                return f"[{v}]"
    return f"[{index}]"


def selector(loc: tuple[Any, ...] | list[Any], raw: Any) -> str:
    """Pydantic/YAML location -> selector path. Discriminator tags are dropped; list items are named by id."""
    out = ""
    node = raw
    for part in loc:
        if part == "[key]":
            continue
        if (
            isinstance(node, dict)
            and isinstance(part, str)
            and part not in node
            and part in (node.get(k) for k in TAG_KEYS)
        ):
            continue
        if isinstance(node, list) and isinstance(part, int):
            out += _item_key(node[part] if part < len(node) else None, part)
            node = node[part] if part < len(node) else None
            continue
        out += f".{part}" if out else str(part)
        node = node.get(part) if isinstance(node, dict) else None
    return out


def placeholders(node: Any, loc: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    found: list[tuple[Any, ...]] = []
    if isinstance(node, str) and node.strip() == REQUIRED:
        found.append(loc)
    elif isinstance(node, dict):
        for k, v in node.items():
            found += placeholders(v, (*loc, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += placeholders(v, (*loc, i))
    return found


def _vocabulary() -> list[str]:
    words: set[str] = set()

    def walk(s: Any) -> None:
        if isinstance(s, dict):
            words.update(s.get("properties", {}).keys())
            for v in s.values():
                walk(v)
        elif isinstance(s, list):
            for v in s:
                walk(v)

    for f in SPEC_FILES.values():
        walk(f.model.model_json_schema())
    return sorted(words)


_VOCAB: list[str] | None = None


def _did_you_mean(word: str) -> str:
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = _vocabulary()
    m = difflib.get_close_matches(word, _VOCAB, n=1, cutoff=0.75)
    return f" (did you mean '{m[0]}'?)" if m else ""


def _translate(fname: str, err: Any, raw: Any) -> Issue:
    loc = selector(err["loc"], raw)
    typ, msg, inp = err["type"], err["msg"], err.get("input")
    if typ == "extra_forbidden":
        last = str(err["loc"][-1])
        return issue("unknown_field", fname, loc, f"'{last}' is not a known field{_did_you_mean(last)}")
    if typ == "missing":
        return issue("required_field", fname, loc, "this field is required")
    if typ == "union_tag_invalid":
        ctx = err.get("ctx", {})
        tag, expected = ctx.get("tag"), ctx.get("expected_tags", "")
        if ctx.get("discriminator") == "'op'":
            from .catalog import op_catalog

            if tag in op_catalog():
                return issue(
                    "op_category_mismatch",
                    fname,
                    loc,
                    f"op '{tag}' does not belong to this category; allowed: {expected}",
                )
            return issue("op_unknown", fname, loc, f"unknown op '{tag}'; allowed here: {expected}")
        return issue("invalid_choice", fname, loc, f"'{tag}' is not allowed here; use one of {expected}")
    if typ == "union_tag_not_found":
        return issue("required_field", fname, loc, f"missing {err.get('ctx', {}).get('discriminator', 'type')}")
    if typ in ("literal_error", "enum"):
        return issue("invalid_choice", fname, loc, msg)
    if typ == "string_type" and isinstance(inp, dict) and len(inp) == 1 and next(iter(inp.values())) is None:
        return issue("template_unquoted", fname, loc, "a template was read as a mapping because it is not quoted")
    if typ == "string_type" and isinstance(inp, int | float) and not isinstance(inp, bool):
        return issue("quote_value", fname, loc, "this must be text; quote the value")
    text = msg.removeprefix("Value error, ")
    return issue("invalid_value", fname, loc, text)


def _covered(loc: str, holes: list[str]) -> bool:
    return any(loc == h or loc.startswith((h + ".", h + "[")) or h.startswith((loc + ".", loc + "[")) for h in holes)


def load_spec(pipeline_dir: Path) -> LoadResult:
    pipeline_dir = Path(pipeline_dir)
    spec_dir = pipeline_dir / SPEC_DIR
    res = LoadResult(pipeline_dir, None)
    models: dict[str, Any] = {}
    for fname, sf in SPEC_FILES.items():
        f = spec_dir / fname
        if not f.is_file():
            res.issues.append(issue("missing_file", fname, "", f"{SPEC_DIR}/{fname} is missing"))
            continue
        try:
            raw = yamlio.to_plain(yamlio.load_path(f)) or {}
        except yamlio.YamlError as exc:
            where = f" (line {exc.line})" if exc.line else ""
            res.issues.append(issue(exc.code, fname, exc.loc, f"{exc}{where}"))
            continue
        if not isinstance(raw, dict):
            res.issues.append(issue("invalid_value", fname, "", "the file must contain a mapping"))
            continue
        res.raw[fname] = raw
        holes = [selector(h, raw) for h in placeholders(raw)]
        for h in holes:
            res.issues.append(issue("required", fname, h, "value not filled in yet"))
        if fname == "pipeline.yaml":
            res.issues += _version_issues(raw)
        try:
            models[fname] = sf.model.model_validate(raw)
        except ValidationError as exc:
            for e in exc.errors():
                i = _translate(fname, e, raw)
                if not _covered(i.loc, holes):
                    res.issues.append(i)
    if len(models) == len(SPEC_FILES):
        res.spec = PipelineSpec(
            path=pipeline_dir,
            pipeline=models["pipeline.yaml"],
            rules=models["rules.yaml"],
            integrations=models["integrations.yaml"],
            security=models["security.yaml"],
            environments=models["environments.yaml"],
            destinations=models["destinations.yaml"],
            raw=res.raw,
        )
    return res


def _version_issues(raw: dict[str, Any]) -> list[Issue]:
    out: list[Issue] = []
    av = raw.get("api_version")
    if av not in (None, REQUIRED, "factory/v1"):
        out.append(
            issue("api_version_mismatch", "pipeline.yaml", "api_version", f"api_version '{av}' is not supported")
        )
    sv = raw.get("schema_version")
    if isinstance(sv, str) and sv.count(".") == 2 and all(p.isdigit() for p in sv.split(".")):
        major, minor, _ = (int(p) for p in sv.split("."))
        if (major, minor) > SUPPORTED_SCHEMA:
            out.append(
                issue(
                    "schema_version_unsupported",
                    "pipeline.yaml",
                    "schema_version",
                    f"schema_version {sv} is newer than this factory supports",
                )
            )
        elif (major, minor) < SUPPORTED_SCHEMA:
            out.append(
                issue(
                    "schema_version_outdated",
                    "pipeline.yaml",
                    "schema_version",
                    f"schema_version {sv} is older than the current 1.0",
                )
            )
    return out
