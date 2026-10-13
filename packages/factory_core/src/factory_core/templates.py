"""Restricted token templates: `{study_id}_{package_id}.zip`, `{seq:04d}`, `{{` / `}}` for literal braces.

A hand-written scanner, never `str.format` (which would allow `{x.__class__}` attribute walks). Which tokens are allowed
depends on the context; the spec validator checks that, this module only parses and renders.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?$")
_FMT = re.compile(r"^0[1-9]d$")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
INT_FORMAT_TOKENS = frozenset({"seq", "rev"})


class TemplateError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Token:
    name: str
    fmt: str | None = None


@dataclass(frozen=True)
class Template:
    source: str
    parts: tuple[str | Token, ...]

    @property
    def tokens(self) -> tuple[Token, ...]:
        return tuple(p for p in self.parts if isinstance(p, Token))

    @property
    def names(self) -> frozenset[str]:
        return frozenset(t.name for t in self.tokens)


def parse_template(source: str) -> Template:
    if not isinstance(source, str):
        raise TemplateError("template_syntax", "a template must be text")
    parts: list[str | Token] = []
    buf: list[str] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c == "{":
            if i + 1 < n and source[i + 1] == "{":
                buf.append("{")
                i += 2
                continue
            end = source.find("}", i + 1)
            if end == -1:
                raise TemplateError("template_syntax", f"unclosed '{{' in template '{source}'")
            body = source[i + 1 : end]
            name, _, fmt = body.partition(":")
            if not _NAME.match(name):
                raise TemplateError(
                    "template_syntax", f"'{{{body}}}' is not a valid token (use lower_snake_case names)"
                )
            if fmt and (not _FMT.match(fmt) or name not in INT_FORMAT_TOKENS):
                raise TemplateError(
                    "template_syntax", f"format '{fmt}' is only allowed as :0Nd on {sorted(INT_FORMAT_TOKENS)}"
                )
            if buf:
                parts.append("".join(buf))
                buf = []
            parts.append(Token(name, fmt or None))
            i = end + 1
            continue
        if c == "}":
            if i + 1 < n and source[i + 1] == "}":
                buf.append("}")
                i += 2
                continue
            raise TemplateError(
                "template_syntax", f"unmatched '}}' in template '{source}' (write '}}}}' for a literal)"
            )
        buf.append(c)
        i += 1
    if buf:
        parts.append("".join(buf))
    return Template(source, tuple(parts))


def render(template: Template, values: Mapping[str, str | int], *, path_safe: bool = False) -> str:
    """Substitute token values. With path_safe, every substituted value must be a safe path segment."""
    out: list[str] = []
    for p in template.parts:
        if isinstance(p, str):
            out.append(p)
            continue
        if p.name not in values:
            raise TemplateError("template_unknown_token", f"no value for token '{p.name}'")
        v = values[p.name]
        if p.fmt:
            if not isinstance(v, int) or isinstance(v, bool):
                raise TemplateError("template_syntax", f"token '{p.name}' needs an integer value")
            text = format(v, p.fmt)
        else:
            text = str(v)
        if path_safe and not _SAFE_VALUE.match(text):
            raise TemplateError("template_unsafe_value", f"value for '{p.name}' is not a safe path segment")
        out.append(text)
    return "".join(out)
