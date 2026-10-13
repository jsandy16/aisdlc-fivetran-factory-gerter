"""Run the owner-approved examples (`test_cases` in rules.yaml) that pure logic can decide.

Supported: validate expressions (pass/fail), naming (built-in template or expression -> value), and
destination.resolve (slot -> allow-list entry, or the expected error). Other rules need the runtime or are custom code
(accepted in Phase 4); their cases are reported as skipped with the reason, never as passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from factory_core.errors import FactoryError
from factory_core.templates import TemplateError, parse_template, render

from factory_core import cel

from .resolve import resolve_slots
from .spec import PipelineSpec


@dataclass(frozen=True)
class CaseResult:
    rule_id: str
    case: str
    status: Literal["pass", "fail", "skipped"]
    detail: str = ""


def _expect_error(expect: dict[str, Any], exc: FactoryError) -> tuple[bool, str]:
    ok = expect.get("error_class") == str(exc.error_class) and expect.get("code", exc.code) == exc.code
    return ok, f"{exc.error_class}/{exc.code}"


def _resolve_case(spec: PipelineSpec, rule: Any, given: dict[str, Any], expect: dict[str, Any]) -> tuple[bool, str]:
    values = {k: str(v) for k, v in (given.get("vars") or {}).items()}
    try:
        res = resolve_slots(
            spec, str(given.get("env", "")), list(rule.implementation.params.slots), given.get("portal") or {}, values
        )
    except FactoryError as exc:
        if "error_class" in expect:
            return _expect_error(expect, exc)
        return False, f"unexpected {exc.error_class}/{exc.code}"
    actual = {slot: r.entry.id for slot, r in res.items()}
    return actual == expect, f"got {actual}"


def _expression_case(rule: Any, given: dict[str, Any], expect: dict[str, Any]) -> tuple[bool, str]:
    act = {"artifacts": given.get("artifacts") or {}, "vars": given.get("vars") or {}, "run": given.get("run") or {}}
    if rule.category == "naming":
        try:
            value = cel.evaluate_value(rule.implementation.cel, act)
        except cel.CelEvalError as exc:
            return False, str(exc)
        return value == expect.get("value"), f"got {value!r}"
    passed, _ = cel.evaluate(rule.implementation.cel, act)
    actual = "pass" if passed else "fail"
    return actual == expect.get("result"), f"got {actual}"


def _naming_case(rule: Any, given: dict[str, Any], expect: dict[str, Any]) -> tuple[bool, str]:
    values: dict[str, str | int] = {**(given.get("run") or {}), **(given.get("vars") or {})}
    try:
        value = render(parse_template(rule.implementation.params.template), values, path_safe=True)
    except TemplateError as exc:
        if "error_class" in expect:
            return expect.get("code") == exc.code, exc.code
        return False, exc.code
    return value == expect.get("value"), f"got {value!r}"


def run_case(spec: PipelineSpec, rule: Any, case: Any) -> CaseResult:
    impl = rule.implementation
    given, expect = dict(case.given), dict(case.expect)
    if impl.kind == "builtin" and impl.op == "destination.resolve":
        ok, detail = _resolve_case(spec, rule, given, expect)
    elif impl.kind == "expression":
        ok, detail = _expression_case(rule, given, expect)
    elif impl.kind == "builtin" and impl.op == "naming.render":
        ok, detail = _naming_case(rule, given, expect)
    else:
        why = "custom rules are tested by their test_impl.py" if impl.kind == "custom" else "needs the runtime"
        return CaseResult(rule.rule_id, case.name, "skipped", why)
    return CaseResult(rule.rule_id, case.name, "pass" if ok else "fail", detail)


def run_owner_tests(spec: PipelineSpec) -> list[CaseResult]:
    return [run_case(spec, r, c) for r in spec.rules.rules for c in r.test_cases]
