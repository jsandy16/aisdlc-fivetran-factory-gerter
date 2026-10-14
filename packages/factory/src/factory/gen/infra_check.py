"""`factory infra check`: read-only proof that what the infra team provides matches the handover contract.

Existence for every provided resource; for provided roles, the IAM policy simulator says whether each action the
contract needs is allowed on its resources. Never creates, changes or deletes anything; needs credentials (boto3).
"""

from __future__ import annotations

import json
from typing import Any

UNCHECKED = ("aws_lambda_event_source_mapping", "aws_eks_pod_identity_association", "aws_sqs_queue_policy")


def _code(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    return str(resp.get("Error", {}).get("Code")) if isinstance(resp, dict) else type(exc).__name__


def _exists(session: Any, rtype: str, name: str) -> bool:
    if rtype == "aws_iam_role":
        session.client("iam").get_role(RoleName=name)
    elif rtype == "aws_sqs_queue":
        session.client("sqs").get_queue_url(QueueName=name)
    elif rtype == "aws_lambda_function":
        session.client("lambda").get_function(FunctionName=name)
    elif rtype == "aws_cloudwatch_log_group":
        groups = session.client("logs").describe_log_groups(logGroupNamePrefix=name).get("logGroups", [])
        return any(g.get("logGroupName") == name for g in groups)
    return True


def _simulate(session: Any, role_arn: str, policy: dict[str, Any]) -> list[str]:
    denied = []
    for st in policy.get("Statement", []):
        actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        resources = [r for r in (st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]])
                     if "${" not in r]  # fmt: skip
        res = session.client("iam").simulate_principal_policy(
            PolicySourceArn=role_arn, ActionNames=actions, ResourceArns=resources or ["*"]
        )
        denied += [f"{r['EvalActionName']} on {r.get('EvalResourceName', '*')}" for r in res["EvaluationResults"]
                   if r["EvalDecision"] != "allowed"]  # fmt: skip
    return denied


def check(session: Any, contract: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    roles: dict[str, str] = {}
    for item in contract["provide"]:
        rtype, addr, name = item["type"], item["address"], item["name"]
        if rtype in UNCHECKED or rtype == "aws_iam_role_policy":
            continue
        try:
            ok = _exists(session, rtype, name)
            status, detail = ("ok", "exists") if ok else ("missing", "not found")
            if ok and rtype == "aws_iam_role":
                roles[addr.split(".", 1)[1]] = session.client("iam").get_role(RoleName=name)["Role"]["Arn"]
        except Exception as exc:
            code = _code(exc)
            missing = code in ("NoSuchEntity", "AWS.SimpleQueueService.NonExistentQueue", "ResourceNotFoundException",
                               "QueueDoesNotExist")  # fmt: skip
            status, detail = ("missing", "not found") if missing else ("error", code)
        out.append({"address": addr, "name": name, "status": status, "detail": detail})
    for item in contract["provide"]:
        if item["type"] != "aws_iam_role_policy":
            continue
        key = item["address"].split(".", 1)[1]
        if key not in roles:
            continue
        try:
            denied = _simulate(session, roles[key], json.loads(item["settings"]["policy"]))
            status, detail = ("ok", "every action allowed") if not denied else ("denied", "; ".join(denied[:5]))
        except Exception as exc:
            status, detail = "error", _code(exc)
        out.append({"address": item["address"], "name": item["name"] or key, "status": status, "detail": detail})
    out += [{"address": i["address"], "name": i["name"] or "", "status": "unchecked",
             "detail": "check in the console (no read API by name)"}
            for i in contract["provide"] if i["type"] in UNCHECKED]  # fmt: skip
    return out
