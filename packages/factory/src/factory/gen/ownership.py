"""Who owns each AWS resource of a pipeline stack: the framework (create) or the infra team (existing).

The generator always computes the full stack, so what the infra team is asked to build is exactly what the framework
would have built (same names, same IAM policies). This pass then, per environment:

* `existing` group: its resources leave `main.tf.json` and go into the handover contract (`infra/<env>.json|md`);
  references to them become `data` lookups, so `terraform plan` stops early if something is missing; and a
  `removed { lifecycle { destroy = false } }` block releases anything this stack managed before, without deleting it.
* `create` group with `adopt: true`: `import` blocks take over resources that already exist.
"""

from __future__ import annotations

import json
import re
from typing import Any

from factory_core.envelope import CommandError

GROUPS: dict[str, tuple[str, ...]] = {
    "roles": ("aws_iam_role", "aws_iam_role_policy"),
    "queues": ("aws_sqs_queue", "aws_sqs_queue_policy"),
    "logs": ("aws_cloudwatch_log_group",),
    "compute": ("aws_lambda_function", "aws_lambda_event_source_mapping", "aws_eks_pod_identity_association"),
}
# resource type -> (name field, attribute renames for the data source)
LOOKUP: dict[str, tuple[str, dict[str, str]]] = {
    "aws_iam_role": ("name", {}),
    "aws_sqs_queue": ("name", {"id": "url"}),
    "aws_cloudwatch_log_group": ("name", {}),
    "aws_lambda_function": ("function_name", {}),
}
_REF = re.compile(r"\$\{(aws_[a-z0-9_]+)\.([A-Za-z0-9_-]+)\.([a-z_]+)\}")


def _group_of(rtype: str) -> str | None:
    return next((g for g, types in GROUPS.items() if rtype in types), None)


def _resolve(value: Any, fn: Any) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_resolve(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, fn) for k, v in value.items()}
    return value


def _import_id(
    rtype: str, key: str, body: dict[str, Any], res: dict[str, Any], account: str, region: str
) -> str | None:
    if rtype in ("aws_iam_role", "aws_cloudwatch_log_group"):
        return str(body["name"])
    if rtype == "aws_lambda_function":
        return str(body["function_name"])
    if rtype == "aws_sqs_queue":
        return f"https://sqs.{region}.amazonaws.com/{account}/{body['name']}"
    if rtype == "aws_sqs_queue_policy":
        queue = res["aws_sqs_queue"].get(key, {})
        return f"https://sqs.{region}.amazonaws.com/{account}/{queue['name']}" if queue else None
    if rtype == "aws_iam_role_policy":
        return f"{res['aws_iam_role'][key]['name']}:{body['name']}"
    return None  # event-source mappings and Pod Identity associations have AWS-generated ids: give import_ids


_VAR = re.compile(r"\$\{var\.([a-z0-9_]+)\}")


def _concrete(rtype: str, attr: str, name: str, account: str, region: str, part: str) -> str:
    """What a Terraform reference means, spelled out for people who build the resource by other means."""
    if rtype == "aws_iam_role":
        return name if attr in ("id", "name") else f"arn:{part}:iam::{account}:role/{name}"
    if rtype == "aws_sqs_queue":
        if attr in ("id", "url"):
            return f"https://sqs.{region}.amazonaws.com/{account}/{name}"
        return f"arn:{part}:sqs:{region}:{account}:{name}"
    if rtype == "aws_lambda_function":
        return f"arn:{part}:lambda:{region}:{account}:function:{name}"
    if rtype == "aws_cloudwatch_log_group":
        return f"arn:{part}:logs:{region}:{account}:log-group:{name}"
    return f"<{rtype} {name} {attr}>"


def apply_ownership(
    main: dict[str, Any], infra: Any, account: str, region: str, part: str = "aws"
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(main.tf.json, ownership.tf.json (may be empty), handover contract)."""
    res: dict[str, dict[str, Any]] = main.get("resource", {})
    existing = {g for g in GROUPS if infra.mode_of(g) == "existing"}
    moved: dict[tuple[str, str], dict[str, Any]] = {}  # (type, key) -> body, for every resource the team provides
    for rtype, items in res.items():
        if _group_of(rtype) in existing:
            for key, body in items.items():
                moved[(rtype, key)] = body
    for g in existing:  # names the infra team used, where they differ from the framework's
        overrides = getattr(infra, g).names
        known = {k for (t, k) in moved if _group_of(t) == g}
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise CommandError(f"infrastructure.{g}.names: no such resource key(s) {unknown}; known: {sorted(known)}")
        for (rtype, key), body in moved.items():
            name_field = LOOKUP.get(rtype, ("name", {}))[0]
            if _group_of(rtype) == g and key in overrides and name_field in body:
                moved[(rtype, key)] = {**body, name_field: overrides[key]}
    data: dict[str, dict[str, Any]] = {}

    def rewrite(text: str) -> str:
        def sub(m: re.Match[str]) -> str:
            rtype, key, attr = m.groups()
            if (rtype, key) not in moved or rtype not in LOOKUP:
                return m.group(0)
            field, renames = LOOKUP[rtype]
            data.setdefault(rtype, {})[key] = {field: moved[(rtype, key)][field]}
            return f"${{data.{rtype}.{key}.{renames.get(attr, attr)}}}"

        return _REF.sub(sub, text)

    kept: dict[str, dict[str, Any]] = {}
    for rtype, items in res.items():
        for key, body in items.items():
            if (rtype, key) in moved:
                continue
            body = _resolve(body, rewrite)
            deps = [d for d in body.get("depends_on", []) if tuple(d.split(".", 1)) not in moved]
            body = {k: v for k, v in body.items() if k != "depends_on"} | ({"depends_on": deps} if deps else {})
            kept.setdefault(rtype, {})[key] = body
    for (rtype, key), body in moved.items():  # every provided resource is looked up, so plan fails early if missing
        if rtype in LOOKUP:
            data.setdefault(rtype, {})[key] = {LOOKUP[rtype][0]: body[LOOKUP[rtype][0]]}
    out = {k: v for k, v in main.items() if k != "resource"}
    if kept:
        out["resource"] = {t: dict(sorted(i.items())) for t, i in sorted(kept.items())}
    if data:
        out["data"] = {t: dict(sorted(i.items())) for t, i in sorted(data.items())}
    removed = [{"from": f"{t}.{k}", "lifecycle": {"destroy": False}} for (t, k) in sorted(moved)]
    imports, cannot = [], []
    for g in sorted(set(GROUPS) - existing):
        grp = getattr(infra, g)
        if not grp.adopt:
            continue
        for rtype in GROUPS[g]:
            for key, body in sorted(res.get(rtype, {}).items()):
                addr = f"{rtype}.{key}"
                iid = grp.import_ids.get(addr) or _import_id(rtype, key, body, res, account, region)
                if iid is None:
                    cannot.append(addr)
                else:
                    imports.append({"to": addr, "id": iid})
    ownership: dict[str, Any] = {}
    if removed:
        ownership["removed"] = removed
    if imports:
        ownership["import"] = imports

    def spell(text: str) -> str:
        def sub(m: re.Match[str]) -> str:
            rtype, key, attr = m.groups()
            body = moved.get((rtype, key)) or res.get(rtype, {}).get(key)
            if body is None or rtype not in LOOKUP:
                return m.group(0)
            return _concrete(rtype, attr, str(body[LOOKUP[rtype][0]]), account, region, part)

        return _VAR.sub(lambda m: f"<{m.group(1)}: from the platform settings>", _REF.sub(sub, text))

    contract = {
        "groups": {g: infra.mode_of(g) for g in sorted(GROUPS)},
        "provide": [
            {"group": _group_of(t), "address": f"{t}.{k}", "type": t, "name": b.get("name") or b.get("function_name"),
             "settings": _resolve(b, spell)}
            for (t, k), b in sorted(moved.items())
        ],
        "adopt_needs_import_ids": cannot,
    }  # fmt: skip
    return out, ownership, contract


def contract_markdown(pid: str, env: str, contract: dict[str, Any]) -> str:
    lines = [f"# Infrastructure handover: `{pid}` in `{env}`", "", "Generated by `factory build`; do not edit.", "",
             "Owner per group (framework = created by this pipeline's Terraform; existing = the infra team provides "
             "it with exactly these settings):", ""]  # fmt: skip
    lines += ["| Group | Owner |", "|---|---|"]
    lines += [f"| {g} | {'existing (infra team)' if m == 'existing' else 'framework'} |"
              for g, m in contract["groups"].items()]  # fmt: skip
    for item in contract["provide"]:
        settings = dict(item["settings"])
        for k in ("policy", "assume_role_policy", "redrive_policy"):
            if isinstance(settings.get(k), str):
                settings[k] = json.loads(settings[k])
        lines += ["", f"## {item['type']} `{item['name']}` ({item['group']})", "", "```json",
                  json.dumps(settings, indent=2, sort_keys=True), "```"]  # fmt: skip
    if contract["adopt_needs_import_ids"]:
        lines += ["", "Adopting these needs `import_ids` (AWS-generated ids): "
                  + ", ".join(f"`{a}`" for a in contract["adopt_needs_import_ids"])]  # fmt: skip
    return "\n".join(lines) + "\n"
