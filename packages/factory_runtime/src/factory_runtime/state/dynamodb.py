"""Run-state in DynamoDB (single table, see the package docstring). Conditional writes guard every idempotent step."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from typing import Any

from factory_core.canonical import canonical_bytes

from .protocol import Record

_CONDITION_FAILED = "ConditionalCheckFailedException"


def _code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    return str(response.get("Error", {}).get("Code", "")) if isinstance(response, dict) else ""


class DynamoRunState:
    def __init__(
        self,
        client: Any,
        table: str,
        pipeline_id: str,
        *,
        ttl_seconds: int = 45 * 86400,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.table = table
        self.pipeline_id = pipeline_id
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    def _pk(self, run_dir: str) -> str:
        return f"{self.pipeline_id}#{run_dir}"

    def _item(self, pk: str, sk: str, doc: dict[str, Any], state: str | None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "pk": {"S": pk},
            "sk": {"S": sk},
            "doc": {"S": canonical_bytes(doc).decode()},
            "ttl": {"N": str(int(self.clock()) + self.ttl_seconds)},
        }
        if state is not None:
            item["state"] = {"S": state}
        return item

    @staticmethod
    def _record(item: dict[str, Any]) -> Record:
        return Record(
            item["sk"]["S"],
            json.loads(item["doc"]["S"]) if "doc" in item else {},
            item["state"]["S"] if "state" in item else None,
        )

    def _get(self, pk: str, sk: str) -> Record | None:
        res = self.client.get_item(TableName=self.table, Key={"pk": {"S": pk}, "sk": {"S": sk}}, ConsistentRead=True)
        item = res.get("Item")
        return self._record(item) if item else None

    def get(self, run_dir: str, sk: str) -> Record | None:
        return self._get(self._pk(run_dir), sk)

    def put(self, run_dir: str, sk: str, doc: dict[str, Any], *, state: str | None = None) -> None:
        self.client.put_item(TableName=self.table, Item=self._item(self._pk(run_dir), sk, doc, state))

    def put_if_absent(
        self, run_dir: str, sk: str, doc: dict[str, Any], *, state: str | None = None
    ) -> tuple[bool, Record]:
        pk = self._pk(run_dir)
        try:
            self.client.put_item(
                TableName=self.table,
                Item=self._item(pk, sk, doc, state),
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True, Record(sk, dict(doc), state)
        except Exception as exc:
            if _code(exc) != _CONDITION_FAILED:
                raise
        existing = self._get(pk, sk)
        if existing is None:  # pragma: no cover - deleted between the two calls (TTL)
            raise RuntimeError("run-state item vanished during a conditional write")
        return False, existing

    def transition(
        self,
        run_dir: str,
        sk: str,
        *,
        allowed_from: Iterable[str | None],
        to_state: str,
        doc: dict[str, Any] | None = None,
    ) -> bool:
        pk = self._pk(run_dir)
        allowed = list(allowed_from)
        conds: list[str] = []
        values: dict[str, Any] = {":to": {"S": to_state}, ":ttl": {"N": str(int(self.clock()) + self.ttl_seconds)}}
        if None in allowed:
            conds.append("attribute_not_exists(#s)")
        for i, s in enumerate(x for x in allowed if x is not None):
            values[f":f{i}"] = {"S": s}
            conds.append(f"#s = :f{i}")
        current = self._get(pk, sk)
        merged = {**(current.doc if current else {}), **(doc or {})}
        values[":doc"] = {"S": canonical_bytes(merged).decode()}
        try:
            self.client.update_item(
                TableName=self.table,
                Key={"pk": {"S": pk}, "sk": {"S": sk}},
                UpdateExpression="SET #s = :to, #d = :doc, #t = :ttl",
                ConditionExpression=" OR ".join(conds) if conds else "attribute_exists(pk)",
                ExpressionAttributeNames={"#s": "state", "#d": "doc", "#t": "ttl"},
                ExpressionAttributeValues=values,
            )
            return True
        except Exception as exc:
            if _code(exc) == _CONDITION_FAILED:
                return False
            raise

    def allocate_seq(self, name: str, run_dir: str) -> int:
        """The run's number in the named sequence; retries get the same number. A lost race leaves a gap."""
        pk = f"{self.pipeline_id}#SEQ#{name}"
        sk = f"RUN#{run_dir}"
        existing = self._get(pk, sk)
        if existing is not None:
            return int(existing.doc["n"])
        res = self.client.update_item(
            TableName=self.table,
            Key={"pk": {"S": pk}, "sk": {"S": "COUNTER"}},
            UpdateExpression="ADD n :one",
            ExpressionAttributeValues={":one": {"N": "1"}},
            ReturnValues="UPDATED_NEW",
        )
        n = int(res["Attributes"]["n"]["N"])
        try:
            self.client.put_item(
                TableName=self.table,
                Item={
                    "pk": {"S": pk},
                    "sk": {"S": sk},
                    "doc": {"S": canonical_bytes({"n": n}).decode()},
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
            return n
        except Exception as exc:
            if _code(exc) != _CONDITION_FAILED:
                raise
        winner = self._get(pk, sk)
        if winner is None:  # pragma: no cover
            raise RuntimeError("sequence record vanished during a conditional write")
        return int(winner.doc["n"])


def create_table(client: Any, table: str) -> None:
    """The table layout (Terraform creates it in AWS; tests and local runs call this)."""
    client.create_table(
        TableName=table,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
