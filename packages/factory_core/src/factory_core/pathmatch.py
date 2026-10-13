"""Path patterns for allow-listed destinations: literal segments, `{var}`, `*` (one segment), trailing `**`.

`{var}` must equal the run's own value (e.g. the study id), so a destination for study A can never receive study B.
Patterns need at least one leading literal segment.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

_LITERAL = re.compile(r"^[A-Za-z0-9._-]+$")
_VAR = re.compile(r"^\{([a-z][a-z0-9_]*)\}$")

SegKind = Literal["literal", "var", "star", "globstar"]


class PatternError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Seg:
    kind: SegKind
    value: str = ""


@dataclass(frozen=True)
class PathPattern:
    source: str
    absolute: bool
    segs: tuple[Seg, ...]

    @property
    def variables(self) -> frozenset[str]:
        return frozenset(s.value for s in self.segs if s.kind == "var")

    @property
    def literal_root(self) -> str:
        out: list[str] = []
        for s in self.segs:
            if s.kind != "literal":
                break
            out.append(s.value)
        return "/".join(out)


def parse_path_pattern(source: str, *, absolute: bool) -> PathPattern:
    if not isinstance(source, str) or not source:
        raise PatternError("destination_pattern_invalid", "pattern is empty")
    if absolute != source.startswith("/"):
        raise PatternError(
            "destination_pattern_invalid",
            "server paths must start with '/'" if absolute else "S3 prefix patterns must not start with '/'",
        )
    body = source[1:] if absolute else source
    if body.endswith("/"):
        body = body[:-1]
    raw = body.split("/") if body else []
    segs: list[Seg] = []
    for i, r in enumerate(raw):
        if r == "**":
            if i != len(raw) - 1:
                raise PatternError("destination_pattern_invalid", "'**' is only allowed as the last segment")
            segs.append(Seg("globstar"))
        elif r == "*":
            segs.append(Seg("star"))
        elif m := _VAR.match(r):
            segs.append(Seg("var", m.group(1)))
        elif r not in (".", "..") and _LITERAL.match(r):
            segs.append(Seg("literal", r))
        else:
            raise PatternError("destination_pattern_invalid", f"invalid pattern segment '{r}'")
    if not segs or segs[0].kind != "literal":
        raise PatternError(
            "destination_pattern_too_broad", "a destination pattern must start with at least one fixed folder name"
        )
    return PathPattern(source, absolute, tuple(segs))


def match(pattern: PathPattern, segments: tuple[str, ...], values: Mapping[str, str]) -> bool:
    i = 0
    for k, seg in enumerate(pattern.segs):
        if seg.kind == "globstar":
            return k == len(pattern.segs) - 1
        if i >= len(segments):
            return False
        actual = segments[i]
        if seg.kind == "literal" and actual != seg.value:
            return False
        if seg.kind == "var" and (seg.value not in values or actual != values[seg.value]):
            return False
        i += 1
    return i == len(segments)


def may_overlap(a: PathPattern, b: PathPattern) -> bool:
    """Conservative: True if some concrete path could match both patterns."""
    for sa, sb in zip(a.segs, b.segs, strict=False):
        if sa.kind == "globstar" or sb.kind == "globstar":
            return True
        if sa.kind == "literal" and sb.kind == "literal" and sa.value != sb.value:
            return False
    if len(a.segs) == len(b.segs):
        return True
    longer, shorter = (a, b) if len(a.segs) > len(b.segs) else (b, a)
    return longer.segs[len(shorter.segs)].kind == "globstar"
