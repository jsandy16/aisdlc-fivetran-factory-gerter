"""Building blocks shared by all spec files."""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

REQUIRED = "<REQUIRED>"  # placeholder the scaffold writes for values only a person can supply


class StrictModel(BaseModel):
    """Every spec object rejects unknown keys, so a typo is an error, not a silent default."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=False)


def _valid_regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:  # pragma: no cover - message is what matters
        raise ValueError(f"not a valid regular expression: {exc}") from exc
    return value


Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=63)]
"""snake_case name. Matches Fivetran's connection-name rule and its destination naming, so names are never rewritten."""

Regex = Annotated[str, AfterValidator(_valid_regex)]
Environment = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,15}$")]
SemVer = Annotated[str, StringConstraints(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")]
IamRoleArn = Annotated[str, StringConstraints(pattern=r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]+$")]


class SecretRef(StrictModel):
    """A secret is never written in a spec. It names a key of the Fivetran connection configuration,
    which CI fills from the secret store at deploy time."""

    secret: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=63)] = Field(
        description="Configuration key that holds the secret, e.g. sftp_private_key"
    )


OnFailPackage = Literal["reject_package", "fail_sync", "warn"]
OnFailRow = Literal["quarantine_row", "reject_package", "fail_sync", "warn"]
OnFailEntity = Literal["reject_package", "reject_entity", "fail_sync", "warn"]
Mode = Literal["enforce", "shadow"]

FivetranType = Literal[
    "STRING", "INT", "LONG", "DOUBLE", "DECIMAL", "BOOLEAN", "NAIVE_DATE", "NAIVE_DATETIME", "UTC_DATETIME", "JSON"
]
