"""Building blocks shared by every spec model."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

REQUIRED = "<REQUIRED>"  # placeholder a starter writes for values only a person can supply


class StrictModel(BaseModel):
    """Unknown keys are errors (a typo is never a silent default); models are immutable once validated."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def ba(label: str, help_text: str = "", **kwargs: Any) -> Any:
    """A field with a plain-English label for BAs (`x-ba-label` in the JSON Schema) and a description."""
    extra = dict(kwargs.pop("json_schema_extra", None) or {})
    extra["x-ba-label"] = label
    return Field(description=help_text or label, json_schema_extra=extra, **kwargs)
