"""Terraform generator (`.tf.json`, one file per pipeline and environment). Offline: validated with a local provider
mirror and checkov (Phase 5); applied in Phase 8.

Per task: an IAM role whose policy comes from the op contracts' `aws_actions`, every resource scoped to what the
spec binds (buckets and prefixes, secrets, the run-state table rows of this pipeline). KMS keys are named by alias in
the specs, which IAM cannot use as a resource, so KMS statements use `Resource: "*"` with a `kms:ResourceAliases`
condition (or `kms:ViaService` for source buckets). No action is ever a wildcard.
Lambda tasks get a container-image function in the platform's private subnets; pod tasks get a role for EKS Pod
Identity (or IRSA) bound to their service account. Event-triggered pipelines get the queue, its DLQ and the
dispatcher function with a partial-batch event source mapping. The team's stores and KMS keys are platform resources
and are referenced, not created.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

from factory_core.pathmatch import parse_path_pattern
from factory_core.templates import parse_template, render
from factory_spec.catalog import Pricing, op_catalog, pricing
from factory_spec.platform import PlatformSettings
from factory_spec.secrets import effective_provider
from factory_spec.spec import PipelineSpec

from factory_core import units

from ..plan.engine import plan
from . import names
from .names import Deploy

AWS_PROVIDER = "6.66.0"
LOG_RETENTION = (
    1,
    3,
    5,
    7,
    14,
    30,
    60,
    90,
    120,
    150,
    180,
    365,
    400,
    545,
    731,
    1096,
    1827,
    2192,
    2557,
    2922,
    3288,
    3653,
)
BASELINE = "runtime"  # statements every task role needs (bundle, staging, audit, run-state, KMS of the stores)
POLL_BUDGET_S = 600
OIDC_SUB = '${replace(var.eks_oidc_provider_arn, "/^.*oidc-provider//", "")}:sub'


def _partition(region: str) -> str:
    return "aws-us-gov" if "-gov-" in region else ("aws-cn" if region.startswith("cn-") else "aws")


class _Ctx:
    def __init__(self, spec: PipelineSpec, platform: PlatformSettings, deploy: Deploy) -> None:
        self.spec, self.platform, self.deploy = spec, platform, deploy
        self.team = spec.pipeline.metadata.team
        self.region = platform.region
        self.part = _partition(self.region)
        self.team_settings = platform.teams[self.team]
        self.account = platform.accounts[self.team_settings.account].account_id
        self.binding = spec.environments.environments[deploy.env]
        self.pid = spec.pipeline_id

    def account_of(self, placement: str) -> str:
        p = self.platform.placements.get(placement)
        if p is None or p.account == "@team":
            return self.account
        return self.platform.accounts[p.account].account_id

    def secret_arns(self, names_: list[str]) -> list[str]:
        out = []
        sm = self.binding.secrets.aws_secrets_manager if self.binding.secrets else None
        tokens = {"env": self.deploy.env, "team": self.team, "pipeline_id": self.pid}
        for n in names_:
            ref = self.spec.integrations.secrets.get(n)
            if ref is None or sm is None or effective_provider(self.spec, self.deploy.env, n) != "aws_secrets_manager":
                continue  # Vault secrets need no IAM permission (IAM auth signs GetCallerIdentity)
            path = sm.prefix + render(parse_template(ref.path), tokens)
            out.append(f"arn:{self.part}:secretsmanager:{self.region}:{self.account}:secret:{path}-??????")
        return sorted(set(out))


def _stmt(actions: list[str], resources: list[str], condition: dict[str, Any] | None = None) -> dict[str, Any]:
    s: dict[str, Any] = {"Effect": "Allow", "Action": sorted(set(actions)), "Resource": sorted(set(resources))}
    if condition:
        s["Condition"] = condition
    return s


def _kms_alias(c: _Ctx, aliases: list[tuple[str, str]], actions: list[str]) -> dict[str, Any]:
    arns = sorted({f"arn:{c.part}:kms:{c.region}:{acct}:{alias}" for acct, alias in aliases})
    return _stmt(actions, ["*"], {"ForAnyValue:StringEquals": {"kms:ResourceAliases": arns}})


def _baseline(c: _Ctx) -> list[dict[str, Any]]:
    stores = c.team_settings.stores
    s3 = f"arn:{c.part}:s3:::"
    table = f"arn:{c.part}:dynamodb:{c.region}:{c.account}:table/{stores.run_state_table}"
    kms = [(c.account, c.team_settings.kms_aliases[k]) for k in sorted(c.team_settings.kms_aliases)]
    return [
        _stmt(["s3:GetObject"], [f"{s3}{stores.bundle_store}/{c.team}/{c.pid}/*"]),
        _stmt(["s3:AbortMultipartUpload", "s3:GetObject", "s3:PutObject"], [f"{s3}{stores.staging_bucket}/{c.pid}/*"]),
        _stmt(["s3:PutObject"], [f"{s3}{stores.audit_store}/{c.pid}/*"]),
        _stmt(
            ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
            [table],
            {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"{c.pid}#*"]}},
        ),
        _kms_alias(c, kms, ["kms:Decrypt", "kms:GenerateDataKey"]),
    ]


def _rule_statements(c: _Ctx, rule: Any) -> list[dict[str, Any]]:
    impl = rule.implementation
    if impl.kind != "builtin":
        return []
    contract = op_catalog()[impl.op]
    out: list[dict[str, Any]] = []
    s3 = f"arn:{c.part}:s3:::"
    entries = _entries(c, rule)
    for a in contract.aws_actions:
        res = a.resource
        if res.startswith(("{staging.", "{run_state.")):
            continue  # in the baseline
        if res == "{entry.object_arns}":
            arns = [
                f"{s3}{e.bucket}/{parse_path_pattern(e.prefix_pattern, absolute=False).literal_root}/*"
                for e in entries
                if e.type == "s3"
            ]
            if arns:
                out.append(_stmt(a.actions, arns))
        elif res == "{entry.kms_arn}":
            aliases = [(c.account_of(e.placement), e.kms_key) for e in entries if e.type == "s3"]
            if aliases:
                out.append(_kms_alias(c, aliases, a.actions))
        elif res == "{entry.secret_arns}":
            secrets = [n for e in entries for n in _entry_secrets(e)]
            arns = c.secret_arns(secrets)
            if arns:
                out.append(_stmt(a.actions, arns))
        elif res in ("{portal.secret_arns}", "{api.secret_arns}"):
            api = (
                c.spec.integrations.portal
                if res.startswith("{portal")
                else next((x for x in c.spec.integrations.apis if x.id == impl.params.api), None)
            )
            arns = c.secret_arns(_auth_secrets(api.auth)) if api else []
            if arns:
                out.append(_stmt(a.actions, arns))
        elif res.startswith("{source."):
            out += _source(c, rule, a.actions, res)
    if impl.op == "fivetran.sync":
        out.append(_stmt(["secretsmanager:GetSecretValue"], ["${var.fivetran_api_secret_arn}"]))
    if impl.op == "sqs.read" and c.spec.pipeline.trigger.type == "sqs" and c.spec.pipeline.trigger.mode == "in_task":
        q = c.binding.queues[c.spec.pipeline.trigger.queue].name
        out.append(
            _stmt(
                ["sqs:ChangeMessageVisibility", "sqs:DeleteMessage", "sqs:GetQueueUrl", "sqs:ReceiveMessage"],
                [f"arn:{c.part}:sqs:{c.region}:{c.account}:{q}"],
            )
        )
    return out


def _entries(c: _Ctx, rule: Any) -> list[Any]:
    impl = rule.implementation
    params = getattr(impl, "params", None)
    slot_id = getattr(params, "slot", None)
    if slot_id is None:
        return []
    slot = next((s for s in c.spec.integrations.destinations if s.id == slot_id), None)
    allowed = c.spec.destinations.destinations.get(c.deploy.env, [])
    if slot is not None and slot.resolve.via == "static":
        return [e for e in allowed if e.id == slot.resolve.entry]
    return [e for e in allowed if slot_id in e.slots]


def _entry_secrets(e: Any) -> list[str]:
    if e.type == "sftp":
        return [e.private_key_secret]
    if e.type == "https":
        return [e.auth.token_secret] if e.auth.type == "bearer" else [e.auth.cert, e.auth.key]
    return []


def _auth_secrets(auth: Any) -> list[str]:
    fields = ("client", "secret", "cert", "key", "ca")
    return [v for f in fields if isinstance(v := getattr(auth, f, None), str)]


def _source(c: _Ctx, rule: Any, actions: list[str], res: str) -> list[dict[str, Any]]:
    src_id: str | None = getattr(rule.implementation.params, "source", None)
    b = c.binding.sources.get(src_id) if src_id else None
    bindings: list[tuple[str, Any]]
    if b is None or src_id is None:
        # s3.download/s3.copy read the objects their listing names: the pipeline's S3 sources
        bindings = [(sid, sb) for sid, sb in sorted(c.binding.sources.items()) if sb.bucket or sb.buckets]
    else:
        bindings = [(src_id, b)]
    s3 = f"arn:{c.part}:s3:::"
    out = []
    for sid, sb in bindings:
        src: Any = next(s for s in c.spec.integrations.sources if s.id == sid)
        buckets = [sb.bucket] if sb.bucket else list(sb.buckets)
        prefixes = [src.location.prefix] if src.location.via == "static" else list(sb.prefix_roots)
        if res == "{source.bucket_arns}":
            out.append(
                _stmt(
                    actions,
                    [f"{s3}{x}" for x in buckets],
                    {"StringLike": {"s3:prefix": sorted(f"{p}*" for p in prefixes)}},
                )
            )
        elif res == "{source.object_arns}":
            out.append(_stmt(actions, [f"{s3}{x}/{p}*" for x in buckets for p in prefixes]))
        elif res == "{source.kms_arns}":
            out.append(_stmt(actions, ["*"], {"StringEquals": {"kms:ViaService": f"s3.{c.region}.amazonaws.com"}}))
    return out


def _merge(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge statements with the same actions and condition; deterministic order."""
    groups: dict[str, dict[str, Any]] = {}
    for s in statements:
        key = json.dumps([s["Action"], s.get("Condition")], sort_keys=True)
        if key in groups:
            groups[key]["Resource"] = sorted(set(groups[key]["Resource"]) | set(s["Resource"]))
        else:
            groups[key] = json.loads(json.dumps(s))
    return [groups[k] for k in sorted(groups)]


def _retention_days(text: str) -> int:
    days = units.parse_retention_days(text)
    return next((d for d in LOG_RETENTION if d >= days), LOG_RETENTION[-1])


def runtime_env(spec: PipelineSpec, platform: PlatformSettings, deploy: Deploy) -> dict[str, str]:
    """A task's FACTORY_* settings (never secrets): the Lambda environment here, the pod's env_vars in the DAG."""
    return _env_vars(_Ctx(spec, platform, deploy), poll=False)


def _env_vars(c: _Ctx, *, poll: bool) -> dict[str, str]:
    stores = c.team_settings.stores
    v = {
        "FACTORY_ENV": c.deploy.env,
        "FACTORY_TEAM": c.team,
        "FACTORY_PIPELINE_ID": c.pid,
        "FACTORY_REGION": c.region,
        "FACTORY_STAGING_BUCKET": stores.staging_bucket,
        "FACTORY_RUN_STATE_TABLE": stores.run_state_table,
        "FACTORY_BUNDLE_STORE": stores.bundle_store,
        "FACTORY_AUDIT_STORE": stores.audit_store,
        "FACTORY_STAGING_KMS_KEY": c.team_settings.kms_aliases.get("staging", ""),
        "FACTORY_AUDIT_KMS_KEY": c.team_settings.kms_aliases.get("audit", ""),
        "FACTORY_IMAGE_DIGEST": c.deploy.image.rsplit("@", 1)[-1],
        "FACTORY_PLACEMENT_ACCOUNTS": json.dumps(
            {k: c.account_of(k) for k in sorted(c.platform.placements) if c.platform.placements[k].account != "@team"},
            sort_keys=True,
        ),
        "FACTORY_WORKDIR": "/tmp/factory",  # noqa: S108 - Lambda ephemeral storage
    }
    uses_vault = any(effective_provider(c.spec, c.deploy.env, n) == "vault" for n in c.spec.integrations.secrets)
    if c.platform.vault and c.binding.secrets and c.binding.secrets.vault and uses_vault:
        v |= {
            "FACTORY_VAULT_ADDRESS": c.platform.vault.address,
            "FACTORY_VAULT_AUTH_MOUNT": c.platform.vault.auth_mount,
            "FACTORY_VAULT_ROLE": c.platform.vault.role_pattern.format(team=c.team, pipeline_id=c.pid),
            "FACTORY_VAULT_IAM_SERVER_ID": c.platform.vault.iam_server_id or "",
        }
    if poll:
        v["FACTORY_POLL_BUDGET_S"] = str(POLL_BUDGET_S)
        v["FACTORY_FIVETRAN_API_SECRET"] = "${var.fivetran_api_secret_arn}"  # noqa: S105 - a reference only
    return {k: v[k] for k in sorted(v) if v[k] != ""}


def generate_terraform(
    spec: PipelineSpec, platform: PlatformSettings, deploy: Deploy, prices: Pricing | None = None
) -> dict[str, Any]:
    c = _Ctx(spec, platform, deploy)
    p = spec.pipeline
    decisions = {d["task_id"]: d for d in plan(spec, prices)["engine_decisions"]}
    retention = _retention_days(spec.security.security_and_access.retention.run_logs)
    pod_identity = platform.identity.default == "pod_identity"
    res: dict[str, dict[str, Any]] = defaultdict(dict)
    variables: dict[str, Any] = {
        "lambda_subnet_ids": {"type": "list(string)", "description": "Private subnets of the team VPC"},
        "lambda_security_group_ids": {"type": "list(string)", "description": "Egress-restricted security groups"},
        "kms_key_arn": {"type": "string", "description": "Team key for Lambda environment and log encryption"},
        "eks_cluster_name": {"type": "string", "description": "Team EKS cluster (pod tasks)", "default": ""},
        "eks_oidc_provider_arn": {
            "type": "string",
            "description": "IRSA provider (when not Pod Identity)",
            "default": "",
        },
        "fivetran_api_secret_arn": {"type": "string", "description": "Platform Fivetran API key secret", "default": ""},
    }
    for t in p.orchestration.tasks:
        engine = decisions[t.task_id]["choice"] if t.engine == "auto" else t.engine
        rules = [spec.rule(r) for r in t.rules]
        statements = _merge(_baseline(c) + [s for r in rules for s in _rule_statements(c, r)])
        key = t.task_id
        role = names.role(c.team, c.pid, t.task_id)
        if engine in ("eks_pod", "eks_pod_spot"):
            sa = names.service_account(c.pid, t.task_id)
            ns = names.namespace(c.team)
            if pod_identity:
                trust = {
                    "Effect": "Allow",
                    "Principal": {"Service": "pods.eks.amazonaws.com"},
                    "Action": ["sts:AssumeRole", "sts:TagSession"],
                }
                res["aws_eks_pod_identity_association"][key] = {
                    "cluster_name": "${var.eks_cluster_name}",
                    "namespace": ns,
                    "service_account": sa,
                    "role_arn": f"${{aws_iam_role.{key}.arn}}",
                }
            else:
                trust = {
                    "Effect": "Allow",
                    "Principal": {"Federated": "${var.eks_oidc_provider_arn}"},
                    "Action": "sts:AssumeRoleWithWebIdentity",
                    "Condition": {"StringEquals": {OIDC_SUB: f"system:serviceaccount:{ns}:{sa}"}},
                }
        else:
            trust = {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}
            fn = names.lambda_function(c.team, c.pid, t.task_id)
            poll = engine in ("mwaa", "fivetran_native", "fivetran_sdk")
            data = any(e.get("seconds_max", 0) > 60 for e in decisions[t.task_id]["estimates"].values())
            size = (prices or pricing()).sizing["data" if data else "control"]
            res["aws_cloudwatch_log_group"][key] = {
                "name": f"/aws/lambda/{fn}",
                "retention_in_days": retention,
                "kms_key_id": "${var.kms_key_arn}",
            }
            res["aws_lambda_function"][key] = {
                "function_name": fn,
                "role": f"${{aws_iam_role.{key}.arn}}",
                "package_type": "Image",
                "image_uri": deploy.image,
                "memory_size": size.lambda_memory_mb,
                "timeout": min(t.timeout, 900),
                "ephemeral_storage": {"size": 1024},
                "environment": {"variables": _env_vars(c, poll=poll)},
                "kms_key_arn": "${var.kms_key_arn}",
                "image_config": {"command": ["factory_runtime.lambda_handler.handle"]},
                "tracing_config": {"mode": "Active"},
                "reserved_concurrent_executions": p.orchestration.dag.max_active_runs,
                "vpc_config": {
                    "subnet_ids": "${var.lambda_subnet_ids}",
                    "security_group_ids": "${var.lambda_security_group_ids}",
                },
                "depends_on": [f"aws_cloudwatch_log_group.{key}"],
            }
            statements.append(
                _stmt(
                    ["logs:CreateLogStream", "logs:PutLogEvents"],
                    [f"arn:{c.part}:logs:{c.region}:{c.account}:log-group:/aws/lambda/{fn}:*"],
                )
            )
            statements.append(
                _stmt(
                    ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces"], ["*"]
                )
            )
        res["aws_iam_role"][key] = {
            "name": role,
            "assume_role_policy": json.dumps({"Version": "2012-10-17", "Statement": [trust]}, sort_keys=True),
            "max_session_duration": 3600,
        }
        res["aws_iam_role_policy"][key] = {
            "name": "runtime",
            "role": f"${{aws_iam_role.{key}.id}}",
            "policy": json.dumps({"Version": "2012-10-17", "Statement": _merge(statements)}, sort_keys=True),
        }
    _dispatcher(c, res, variables, retention)
    providers: dict[str, Any] = {
        "terraform": {
            "required_version": ">= 1.9",
            "required_providers": {"aws": {"source": "hashicorp/aws", "version": AWS_PROVIDER}},
        },
        "provider": {
            "aws": {
                "region": c.region,
                "allowed_account_ids": [c.account],
                "default_tags": {
                    "tags": {"factory:pipeline": c.pid, "factory:team": c.team, "factory:env": deploy.env}
                },
            }
        },
    }
    main = {"variable": variables, "resource": {k: dict(sorted(v.items())) for k, v in sorted(res.items())}}
    # two files: Terraform reads both; checkov scans main.tf.json (its JSON parser rejects the provider blocks)
    return {"providers.tf.json": providers, "main.tf.json": main}


def _dispatcher(c: _Ctx, res: dict[str, dict[str, Any]], variables: dict[str, Any], retention: int) -> None:
    trig = c.spec.pipeline.trigger
    if trig.type not in ("sqs", "s3_event") or (trig.type == "sqs" and trig.mode != "dispatch"):
        return
    qname = c.binding.queues[trig.queue].name
    fn = names.dispatcher_function(c.team, c.pid)
    mwaa_account = c.platform.accounts[c.platform.placements["mwaa"].account].account_id
    trigger_role = f"arn:{c.part}:iam::{mwaa_account}:role/" + c.platform.mwaa.trigger_role_pattern.format(team=c.team)
    q_arn = f"arn:{c.part}:sqs:{c.region}:{c.account}:{qname}"
    res["aws_sqs_queue"]["dlq"] = {
        "name": f"{qname}-dlq",
        "message_retention_seconds": 1209600,
        "kms_master_key_id": "${var.kms_key_arn}",
    }
    res["aws_sqs_queue"]["trigger"] = {
        "name": qname,
        "visibility_timeout_seconds": 360,
        "message_retention_seconds": 1209600,
        "kms_master_key_id": "${var.kms_key_arn}",
        "redrive_policy": json.dumps(
            {"deadLetterTargetArn": "${aws_sqs_queue.dlq.arn}", "maxReceiveCount": trig.max_receive_count}
        ),
    }
    if trig.type == "s3_event":
        buckets = sorted({b for sb in c.binding.sources.values() for b in ([sb.bucket] if sb.bucket else sb.buckets)})
        res["aws_sqs_queue_policy"]["trigger"] = {
            "queue_url": "${aws_sqs_queue.trigger.id}",
            "policy": json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "s3.amazonaws.com"},
                            "Action": "sqs:SendMessage",
                            "Resource": q_arn,
                            "Condition": {
                                "ArnLike": {"aws:SourceArn": [f"arn:{c.part}:s3:::{b}" for b in buckets]},
                                "StringEquals": {"aws:SourceAccount": c.account},
                            },
                        }
                    ],
                },
                sort_keys=True,
            ),
        }
    sqs_actions = ["sqs:ChangeMessageVisibility", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ReceiveMessage"]
    eni = ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces"]
    log_arn = f"arn:{c.part}:logs:{c.region}:{c.account}:log-group:/aws/lambda/{fn}:*"
    statements = _merge(
        [
            *_baseline(c),
            _stmt(sqs_actions, [q_arn]),
            _stmt(["sts:AssumeRole"], [trigger_role]),
            _stmt(["logs:CreateLogStream", "logs:PutLogEvents"], [log_arn]),
            _stmt(eni, ["*"]),
        ]
    )
    trust = {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}
    res["aws_iam_role"]["dispatcher"] = {
        "name": fn,
        "max_session_duration": 3600,
        "assume_role_policy": json.dumps({"Version": "2012-10-17", "Statement": [trust]}, sort_keys=True),
    }
    res["aws_iam_role_policy"]["dispatcher"] = {
        "name": "dispatcher",
        "role": "${aws_iam_role.dispatcher.id}",
        "policy": json.dumps({"Version": "2012-10-17", "Statement": statements}, sort_keys=True),
    }
    env = _env_vars(c, poll=False) | {
        "FACTORY_BUNDLE_URI": c.deploy.bundle_uri,
        "FACTORY_BUNDLE_SHA256": c.deploy.bundle_sha256,
        "FACTORY_DAG_ID": names.dag_id(c.pid, c.spec.pipeline.metadata.spec_version),
        "FACTORY_MWAA_ENV": c.platform.mwaa.environment_name,
        "FACTORY_TRIGGER_ROLE_ARN": trigger_role,
    }
    res["aws_cloudwatch_log_group"]["dispatcher"] = {
        "name": f"/aws/lambda/{fn}",
        "retention_in_days": retention,
        "kms_key_id": "${var.kms_key_arn}",
    }
    res["aws_lambda_function"]["dispatcher"] = {
        "function_name": fn,
        "role": "${aws_iam_role.dispatcher.arn}",
        "package_type": "Image",
        "image_uri": c.deploy.image,
        "memory_size": 512,
        "timeout": 60,
        "image_config": {"command": ["factory_runtime.dispatch.handle"]},
        "environment": {"variables": dict(sorted(env.items()))},
        "kms_key_arn": "${var.kms_key_arn}",
        "tracing_config": {"mode": "Active"},
        "reserved_concurrent_executions": 5,
        "vpc_config": {
            "subnet_ids": "${var.lambda_subnet_ids}",
            "security_group_ids": "${var.lambda_security_group_ids}",
        },
        "dead_letter_config": {"target_arn": "${aws_sqs_queue.dlq.arn}"},
        "depends_on": ["aws_cloudwatch_log_group.dispatcher"],
    }
    res["aws_lambda_event_source_mapping"]["trigger"] = {
        "event_source_arn": "${aws_sqs_queue.trigger.arn}",
        "function_name": "${aws_lambda_function.dispatcher.arn}",
        "batch_size": 10,
        "function_response_types": ["ReportBatchItemFailures"],
    }


def egress(spec: PipelineSpec, env: str) -> dict[str, list[str]]:
    """Per task: the host:port pairs its ops may reach (input to the platform's egress allow-list, ADR-0016)."""
    out: dict[str, list[str]] = {}
    for t in spec.pipeline.orchestration.tasks:
        hosts: set[str] = set()
        for rid in t.rules:
            rule: Any = spec.rule(rid)
            impl = rule.implementation
            if impl.kind != "builtin":
                continue
            c = op_catalog()[impl.op]
            binding = spec.environments.environments[env]
            for eg in c.egress:
                if eg.host == "{entry.host}":
                    allowed = spec.destinations.destinations.get(env, [])
                    slot = getattr(impl.params, "slot", None)
                    hosts |= {f"{e.host}:{e.port}" for e in allowed if e.type != "s3" and slot in e.slots}
                elif eg.host in ("{portal.host}", "{api.host}"):
                    api = binding.portal if eg.host == "{portal.host}" else binding.apis.get(impl.params.api)
                    if api is not None:
                        u = urlsplit(api.base_url)
                        hosts.add(f"{u.hostname}:{u.port or 443}")
                else:
                    hosts.add(f"{eg.host}:{eg.port}")
        out[t.task_id] = sorted(hosts)
    return out
