"""Change request M4: notify.publish sends ids and a subject (never data) to a topic declared by configuration."""

from __future__ import annotations

import json
from types import SimpleNamespace as Ns

import boto3
import pytest
from factory_core.errors import ConfigError
from factory_runtime.ops.notify import notify_publish

REGION = "eu-west-1"


def _ctx(resources: dict[str, str], subject: str = "Package {package_id} delivered") -> Ns:
    sns = boto3.client("sns", region_name=REGION)
    return Ns(params=Ns(target="ops_topic", subject=subject),
              services=Ns(config=Ns(resources=resources), aws=Ns(client=lambda name: sns)),
              run=Ns(env="dev", pipeline_id="p", run_key_hash="abc123def456",
                     naming_builtins=lambda: {"pipeline_id": "p", "team": "t", "env": "dev", "run_date": "2026-10-14"}),
              vars=Ns(strings=lambda: {"package_id": "PKG0042"}), rule=Ns(rule_id="r_notify"))  # fmt: skip


def test_publishes_ids_and_subject_only(aws: None) -> None:
    sns, sqs = boto3.client("sns", region_name=REGION), boto3.client("sqs", region_name=REGION)
    topic = sns.create_topic(Name="ops")["TopicArn"]
    q = sqs.create_queue(QueueName="inbox")["QueueUrl"]
    q_arn = sqs.get_queue_attributes(QueueUrl=q, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=q_arn, Attributes={"RawMessageDelivery": "true"})
    out = notify_publish(_ctx({"ops_topic": topic}))
    assert out[0]
    (msg,) = sqs.receive_message(QueueUrl=q)["Messages"]
    assert json.loads(msg["Body"]) == {"env": "dev", "pipeline_id": "p", "rule_id": "r_notify", "run": "abc123def456",
                                       "subject": "Package PKG0042 delivered"}  # fmt: skip


def test_undeployed_target_is_a_config_error(aws: None) -> None:
    with pytest.raises(ConfigError) as e:
        notify_publish(_ctx({}))
    assert e.value.code == "resource_unbound"
