from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Record:
    sk: str
    doc: dict[str, Any] = field(default_factory=dict)
    state: str | None = None


class RunState(Protocol):
    """All writes that guard idempotency are conditional; readers always read consistently."""

    def get(self, run_dir: str, sk: str) -> Record | None: ...

    def put(self, run_dir: str, sk: str, doc: dict[str, Any], *, state: str | None = None) -> None: ...

    def put_if_absent(
        self, run_dir: str, sk: str, doc: dict[str, Any], *, state: str | None = None
    ) -> tuple[bool, Record]: ...

    def transition(
        self,
        run_dir: str,
        sk: str,
        *,
        allowed_from: Iterable[str | None],
        to_state: str,
        doc: dict[str, Any] | None = None,
    ) -> bool: ...

    def allocate_seq(self, name: str, run_dir: str) -> int: ...
