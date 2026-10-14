"""Alert routing by configuration: `observability.alerts` + the environment's `alert_channels` become catalog resources.

Per failure class with a channel: an alarm on the runtime's `TaskFailed` metric (dimensions pipeline_id and
error_class) that notifies the channel's topic. An `sns` channel names an existing topic (looked up, never created);
an `email` or `slack` channel gets a topic of its own (and an email subscription). A pipeline started by messages also
gets an alarm on its dead-letter queue, routed like configuration failures.
"""

from __future__ import annotations

from typing import Any

from factory_spec.resources import Instance, default_name

CLASSES = ("transient", "data", "config", "security")


def alert_instances(spec: Any, env: str) -> list[Instance]:
    obs = spec.pipeline.observability.alerts
    binding = spec.environments.environments[env]
    team, pid = spec.pipeline.metadata.team, spec.pipeline_id
    out: list[Instance] = []
    topic_of: dict[str, str] = {}

    def topic(channel_id: str) -> str | None:
        ch = binding.alert_channels.get(channel_id)
        if ch is None:
            return None
        if channel_id not in topic_of:
            tid = f"alerts_{channel_id}"
            if ch.type == "sns":
                out.append(Instance(tid, "sns_topic", {}, name=ch.topic, group="alerts", force_existing=True))
            else:
                out.append(Instance(tid, "sns_topic", {"display_name": f"{pid} alerts"}, group="alerts",
                                    name=default_name(team, pid, f"alerts-{channel_id}") + "-<env>"))  # fmt: skip
                if ch.type == "email":
                    sub = {"topic": tid, "protocol": "email", "endpoint": ch.address}
                    out.append(Instance(f"{tid}_email", "sns_subscription", sub, group="alerts"))
            topic_of[channel_id] = tid
        return topic_of[channel_id]

    for cls in CLASSES:
        channel = getattr(obs, cls)
        tid = topic(channel) if channel != "none" else None
        if tid is None:
            continue
        params = {"namespace": "PipelineFactory", "metric": "TaskFailed", "notify": tid,
                  "dimensions": {"pipeline_id": pid, "error_class": cls},
                  "description": f"{pid}: a task ended with a {cls} failure"}  # fmt: skip
        name = default_name(team, pid, f"{cls}-failures") + "-<env>"
        out.append(Instance(f"alarm_{cls}", "cloudwatch_metric_alarm", params, name=name, group="alerts"))
    trig = spec.pipeline.trigger
    dispatched = trig.type == "s3_event" or (trig.type == "sqs" and trig.mode == "dispatch")
    config_topic = topic(obs.config) if obs.config != "none" else None
    if dispatched and config_topic:
        dlq = f"{binding.queues[trig.queue].name}-dlq"
        out.append(Instance("alarm_dlq", "cloudwatch_metric_alarm", {
            "namespace": "AWS/SQS", "metric": "ApproximateNumberOfMessagesVisible", "dimensions": {"QueueName": dlq},
            "statistic": "Maximum", "notify": config_topic,
            "description": f"{pid}: messages reached the dead-letter queue"},
            name=default_name(team, pid, "dead-letters") + "-<env>", group="alerts"))  # fmt: skip
    return out


def user_instances(spec: Any, env: str) -> list[Instance]:
    return [Instance(iid, r.type, dict(r.params), name=r.name)
            for iid, r in sorted(spec.environments.resources.items())
            if r.environments is None or env in r.environments]  # fmt: skip
