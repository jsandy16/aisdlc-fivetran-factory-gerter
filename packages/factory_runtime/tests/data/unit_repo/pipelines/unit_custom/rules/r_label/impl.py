"""r_label: label a file name with its study and length (synthetic example of a custom rule)."""

from __future__ import annotations

import json

from factory_runtime.plugin_api import CustomRule, PluginContext, PluginInputs, PluginOutputs, PluginParams, RuleFailed


class Params(PluginParams):
    pass


class Inputs(PluginInputs):
    file_name: str


class Outputs(PluginOutputs):
    label: str


class Rule(CustomRule[Params, Inputs, Outputs]):
    side_effects = frozenset({"writes_workdir"})

    def run(self, ctx: PluginContext, params: Params, inputs: Inputs) -> Outputs:
        stem = inputs.file_name.rsplit(".", 1)[0]
        if "_" not in stem:
            raise RuleFailed("the file name has no study prefix")
        study, rest = stem.split("_", 1)
        label = f"{study}:{rest}:{len(inputs.file_name)}"
        ctx.write_file("label.json", json.dumps({"label": label, "at": ctx.now()}).encode())
        ctx.log("labelled", length=len(inputs.file_name))
        return Outputs(label=label)
