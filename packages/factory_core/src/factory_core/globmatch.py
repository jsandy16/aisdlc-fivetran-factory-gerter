"""One portable glob dialect for object keys and paths (the same on Python 3.11-3.13, unlike fnmatch/glob.translate).

- `*` any characters within one path segment (not `/`); `?` one character, not `/`;
- `[abc]`, `[a-z]`, `[!abc]` one character from (not from) a class, never `/`;
- `**` as a whole segment: zero or more segments (`**/x`, `a/**`, `a/**/b`); `**` inside a segment is an error;
- everything else is literal. Dot files are not special. Matching is case-sensitive and covers the whole path.
"""

from __future__ import annotations

import re
from functools import lru_cache


class GlobError(ValueError):
    code = "glob_invalid"


def _segment(seg: str, pattern: str) -> str:
    out: list[str] = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "*":
            if i + 1 < n and seg[i + 1] == "*":
                raise GlobError(f"'**' must be a whole path segment in '{pattern}'")
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = seg.find("]", i + 2 if i + 1 < n and seg[i + 1] in "!^" else i + 1)
            if end == -1:
                raise GlobError(f"unclosed '[' in '{pattern}'")
            body = seg[i + 1 : end]
            neg = body[:1] in ("!", "^")
            body = body[1:] if neg else body
            if not body:
                raise GlobError(f"empty character class in '{pattern}'")
            cls = re.escape(body).replace(r"\-", "-")
            out.append(f"[^/{cls}]" if neg else f"(?!/)[{cls}]")
            i = end
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


@lru_cache(maxsize=1024)
def compile_glob(pattern: str) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern or pattern.startswith("/") or "\\" in pattern:
        raise GlobError(f"'{pattern}' is not a relative glob (no leading '/', no backslash)")
    segs = pattern.split("/")
    if any(s == "" for s in segs):
        raise GlobError(f"empty path segment in '{pattern}'")
    parts: list[str] = []
    for idx, s in enumerate(segs):
        last = idx == len(segs) - 1
        if s == "**":
            parts.append(".*" if last else "(?:[^/]+/)*")
        else:
            parts.append(_segment(s, pattern) + ("" if last else "/"))
    return re.compile("".join(parts) + r"\Z", re.DOTALL)


def glob_match(pattern: str, path: str) -> bool:
    return compile_glob(pattern).match(path) is not None


def check_glob(pattern: str) -> None:
    compile_glob(pattern)
