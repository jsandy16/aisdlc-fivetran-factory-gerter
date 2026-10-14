"""notify.publish: tell people or systems that something happened; ids and a subject only, never data.

The target is a resource declared by configuration (environments.yaml → resources, an sns_topic); the deployed task
finds its ARN in FACTORY_RESOURCES. The message carries the pipeline, environment, run (hashed key), rule and subject.
"""

from __future__ import annotations

import json
from typing import Any

from factory_core.errors import ConfigError
from factory_core.templates import TemplateError, parse_template, render

from . import op


@op("notify.publish")
def notify_publish(ctx: Any) -> list[Any]:
    target = ctx.params.target
    arn = ctx.services.config.resources.get(target)
    if not arn:
        raise ConfigError("resource_unbound", f"resource '{target}' is not deployed in {ctx.run.env}")
    try:
        subject = render(parse_template(ctx.params.subject), {**ctx.run.naming_builtins(), **ctx.vars.strings()})
    except TemplateError as exc:
        raise ConfigError("notify_subject_invalid", f"the subject cannot be rendered ({exc.code})") from None
    message = {"pipeline_id": ctx.run.pipeline_id, "env": ctx.run.env, "run": ctx.run.run_key_hash,
               "rule_id": ctx.rule.rule_id, "subject": subject}  # fmt: skip
    body = json.dumps(message, sort_keys=True)
    res = ctx.services.aws.client("sns").publish(TopicArn=arn, Subject=subject[:100], Message=body)
    return [str(res.get("MessageId", ""))]
