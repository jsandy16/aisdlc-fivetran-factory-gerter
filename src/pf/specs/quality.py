"""quality.yaml — checks that need the whole landed table, run after each sync on the lake (Athena)."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, StringConstraints, field_validator

from .common import Identifier, StrictModel
from .rules import RuleId


class _LakeCheck(StrictModel):
    id: RuleId
    entity: Identifier
    description: str = Field(min_length=3)
    severity: Literal["blocker", "warning"] = Field(
        default="blocker", description="blocker = table is not certified for consumers (a human-owned decision)"
    )


class LakeRowCount(_LakeCheck):
    kind: Literal["row_count"]
    min: int = Field(default=1, ge=0)
    max: int | None = Field(default=None, ge=1)


class LakeUnique(_LakeCheck):
    kind: Literal["unique"]
    columns: list[Identifier] = Field(min_length=1)


class LakeNotNull(_LakeCheck):
    kind: Literal["not_null"]
    column: Identifier


class LakeFreshness(_LakeCheck):
    kind: Literal["freshness"]
    max_hours: int = Field(ge=1, description="Newest _fivetran_synced must be within this many hours")


class LakeReferential(_LakeCheck):
    kind: Literal["referential"]
    column: Identifier
    ref_entity: Identifier
    ref_column: Identifier


class LakeReconciliation(_LakeCheck):
    """Rows in the table must equal rows the connector reported as emitted (pf_package_audit)."""

    kind: Literal["reconciliation"]


class LakeSqlAssert(_LakeCheck):
    """Owner-reviewed SELECT that returns one integer column `failed` (0 = pass)."""

    kind: Literal["sql_assert"]
    query: Annotated[str, StringConstraints(min_length=10)]

    @field_validator("query")
    @classmethod
    def _select_only(cls, q: str) -> str:
        import re

        body = q.strip().rstrip(";")
        if not re.match(r"(?is)^\s*(with|select)\b", body) or ";" in body:
            raise ValueError("sql_assert must be a single SELECT (or WITH ... SELECT) statement")
        if re.search(r"(?i)\b(insert|update|delete|merge|drop|create|alter|grant|revoke|unload|call)\b", body):
            raise ValueError("sql_assert must not modify data or objects")
        return q


LakeCheck = Annotated[
    Union[LakeRowCount, LakeUnique, LakeNotNull, LakeFreshness, LakeReferential, LakeReconciliation, LakeSqlAssert],
    Field(discriminator="kind"),
]


class QualityFile(StrictModel):
    """quality.yaml"""

    checks: list[LakeCheck] = Field(default_factory=list)
    ticket_route: str = Field(default="", description="Where failing checks are routed (team or queue)")
