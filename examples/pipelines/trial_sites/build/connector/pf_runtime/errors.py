"""Error taxonomy. Each class decides what happens next, so behaviour never depends on message text."""
from __future__ import annotations


class PfRuntimeError(Exception):
    """Base class. Messages never contain data values."""


class TransientError(PfRuntimeError):
    """Network blips, throttling, dropped connections: retried with exponential backoff."""


class ConfigError(PfRuntimeError):
    """Missing secret, bad credentials, unpinned host: fail the sync with a clear message; retrying cannot help."""


class PackageRejected(PfRuntimeError):
    """A delivery broke a rule whose action is reject_package. Nothing from it is emitted."""

    def __init__(self, rule_id: str, reason: str):
        super().__init__(f"{rule_id}: {reason}")
        self.rule_id = rule_id
        self.reason = reason


class EntityRejected(PfRuntimeError):
    def __init__(self, rule_id: str, reason: str):
        super().__init__(f"{rule_id}: {reason}")
        self.rule_id = rule_id
        self.reason = reason


class SyncFailed(PfRuntimeError):
    """A rule whose action is fail_sync fired, or retries were exhausted. The sync fails; state is not advanced."""

    def __init__(self, rule_id: str, reason: str):
        super().__init__(f"{rule_id}: {reason}")
        self.rule_id = rule_id
        self.reason = reason
