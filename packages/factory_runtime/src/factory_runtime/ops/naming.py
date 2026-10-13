"""naming.render: render a file name from a template (path-safe values only; {seq} from run-state)."""

from __future__ import annotations

from typing import Any

from factory_core.errors import SecurityError
from factory_core.templates import TemplateError, parse_template, render

from . import op


@op("naming.render")
def naming_render(ctx: Any) -> list[Any]:
    template = parse_template(ctx.params.template)
    values: dict[str, str | int] = {**ctx.run.naming_builtins(), **ctx.vars.strings()}
    if "seq" in template.names:
        values["seq"] = ctx.services.state.allocate_seq(f"{ctx.rule.rule_id}", ctx.run.run_dir)
    if "rev" in template.names:
        values["rev"] = 0  # forced reruns (rev > 0) arrive with a later minor version
    try:
        name = render(template, values, path_safe=True)
    except TemplateError as exc:
        raise SecurityError("name_unsafe", f"the name cannot be rendered safely ({exc.code})") from None
    return [name]
