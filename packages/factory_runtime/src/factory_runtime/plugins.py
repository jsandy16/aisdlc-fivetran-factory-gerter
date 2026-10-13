"""Custom rules (rules/<rule_id>/impl.py): loaded, verified and run through the plugin API (ADR-0020).

Loading: the file's sha256 must equal the reviewed `code_sha256` in rules.yaml (security, 77, otherwise); the import
allow-list and banned names are checked again (the bundle could have been built from an unchecked tree); then the
module is executed once per code hash and must define exactly one `Rule(CustomRule[Params, Inputs, Outputs])`.
Running: inputs are validated into the plugin's Inputs model, outputs must be the plugin's Outputs model and match the
declared output kinds, and every context call must be covered by the declared side effects (security otherwise).
`RuleFailed` from the plugin is a check failure (on_failure applies); any other exception is an internal error.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
import threading
import types
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory_core.errors import ConfigError, SecurityError
from factory_spec.artifact_models import ARTIFACT_MODELS
from factory_spec.imports import check_plugin_source
from pydantic import BaseModel, ValidationError

from .ops import CheckFailed
from .plugin_api import SIDE_EFFECTS, CustomRule, PluginContext, PluginInputs, PluginOutputs, PluginParams, RuleFailed
from .transports.s3 import open_object

_LOCK = threading.Lock()
_LOADED: dict[str, Plugin] = {}


@dataclass(frozen=True)
class Plugin:
    cls: type[CustomRule[Any, Any, Any]]
    params: type[PluginParams]
    inputs: type[PluginInputs]
    outputs: type[PluginOutputs]
    side_effects: frozenset[str]


def _types(cls: type) -> tuple[type, ...]:
    for base in getattr(cls, "__orig_bases__", ()):
        if typing.get_origin(base) is CustomRule:
            return typing.get_args(base)
    return ()


def load_plugin(code_dir: Path, rule_id: str, code_sha256: str) -> Plugin:
    path = code_dir / "rules" / rule_id / "impl.py"
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise ConfigError("custom_rule_code_missing", f"rules/{rule_id}/impl.py is not in the bundle") from None
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), code_sha256):
        raise SecurityError("custom_rule_code_mismatch", f"rules/{rule_id}/impl.py is not the reviewed code")
    with _LOCK:
        if code_sha256 in _LOADED:
            return _LOADED[code_sha256]
        source = data.decode("utf-8")
        findings = check_plugin_source(source)
        if findings:
            first = findings[0]
            raise SecurityError(
                "custom_rule_import_forbidden", f"rules/{rule_id}/impl.py line {first.line}: {first.msg}"
            )
        name = f"factory_plugins.{rule_id}_{code_sha256[:16]}"
        module = types.ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module  # pydantic resolves the plugin's (postponed) annotations through its module
        try:
            exec(compile(source, str(path), "exec"), module.__dict__)  # noqa: S102 - reviewed, hash-checked plugin code
        except Exception as exc:
            del sys.modules[name]
            raise ConfigError(
                "custom_rule_load_failed", f"rules/{rule_id}/impl.py failed to load ({type(exc).__name__})"
            ) from exc
        rule_cls = getattr(module, "Rule", None)
        if not (isinstance(rule_cls, type) and issubclass(rule_cls, CustomRule)):
            raise ConfigError("custom_rule_contract_invalid", f"rules/{rule_id}/impl.py defines no Rule(CustomRule)")
        args = _types(rule_cls)
        if len(args) != 3 or not (
            issubclass(args[0], PluginParams)
            and issubclass(args[1], PluginInputs)
            and issubclass(args[2], PluginOutputs)
        ):
            raise ConfigError("custom_rule_contract_invalid", "Rule must be CustomRule[Params, Inputs, Outputs]")
        declared = frozenset(rule_cls.side_effects)
        if not declared <= SIDE_EFFECTS:
            raise ConfigError("custom_rule_contract_invalid", f"unknown side effects {sorted(declared - SIDE_EFFECTS)}")
        plugin = Plugin(rule_cls, args[0], args[1], args[2], declared)
        _LOADED[code_sha256] = plugin
        return plugin


def _check_output(kind: str, value: Any, name: str) -> None:
    model = ARTIFACT_MODELS.get(kind)
    ok = isinstance(value, model) if model is not None else kind == "value" and isinstance(value, (str, int, bool))
    if not ok or isinstance(value, float):
        raise ConfigError("custom_rule_output_invalid", f"output '{name}' is not a {kind}")


def run_custom_rule(ctx: Any) -> list[Any]:
    rule = ctx.rule
    impl = rule.implementation
    plugin = load_plugin(ctx.spec.path, rule.rule_id, impl.code_sha256)
    try:
        inputs = plugin.inputs.model_validate({n: ctx.artifacts.get(n).value for n in rule.inputs})
        params = plugin.params()
    except ValidationError as exc:
        fields = sorted({".".join(map(str, e["loc"])) for e in exc.errors(include_input=False)})
        raise ConfigError("custom_rule_inputs_invalid", f"inputs do not fit the rule's contract: {fields}") from None
    s3 = ctx.services.aws.client("s3")
    pctx = PluginContext(
        run_date=ctx.run.run_date,
        run_key_hash=ctx.run.run_key_hash,
        now=ctx.now(),
        workdir=ctx.workdir,
        log=ctx.log,
        opener=lambda pkg: open_object(s3, pkg.bucket, pkg.key, size=pkg.size, what="the staged package"),
    )
    try:
        out = plugin.cls().run(pctx, params, inputs)
    except RuleFailed as failed:
        raise CheckFailed("custom_rule_failed", f"{rule.rule_id}: {failed}") from None
    undeclared = pctx.used - plugin.side_effects
    if undeclared:
        raise SecurityError("custom_rule_side_effect_undeclared", f"{rule.rule_id} used {sorted(undeclared)}")
    if not isinstance(out, plugin.outputs):
        raise ConfigError("custom_rule_output_invalid", f"{rule.rule_id} did not return its Outputs model")
    values: list[Any] = []
    for name in rule.outputs:
        if name not in type(out).model_fields:
            raise ConfigError("custom_rule_output_invalid", f"{rule.rule_id} has no output '{name}'")
        value = getattr(out, name)
        _check_output(impl.output_kinds.get(name, "value"), value, name)
        values.append(value)
    return values


def plugin_contract(plugin: Plugin) -> dict[str, Any]:
    """What a plugin declares (for review records and the IAM generator)."""

    def name(t: Any) -> str:
        return t.__name__ if isinstance(t, type) else str(t).replace("typing.", "")

    def fields(model: type[BaseModel]) -> dict[str, str]:
        return {k: name(v.annotation) for k, v in model.model_fields.items()}

    return {
        "inputs": fields(plugin.inputs),
        "outputs": fields(plugin.outputs),
        "side_effects": sorted(plugin.side_effects),
        "aws_actions": list(plugin.cls.aws_actions),
    }
