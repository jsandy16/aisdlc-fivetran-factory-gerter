"""Run-state: the run pin, checkpoints, dispatcher states, quarantine records and sequence numbers.

One DynamoDB table per team account (platform settings `stores.run_state_table`), single-table design:

    pk = "<pipeline_id>#<run_dir>"         sk = PIN | TASK#<task> | RULE#<task>#<rule> | QUAR#<task>#<rule> | DISP | ...
    pk = "<pipeline_id>#SEQ#<name>"        sk = COUNTER (attribute n) | RUN#<run_dir> (the number given to that run)

Items hold a canonical JSON `doc`, an optional `state` (dispatcher state machine) and a `ttl` (replay window plus a
margin). The leading `<pipeline_id>#` lets IAM restrict each workload role to its own pipeline's items.
"""

from __future__ import annotations

from .memory import MemoryRunState
from .protocol import Record, RunState

__all__ = ["MemoryRunState", "Record", "RunState"]
