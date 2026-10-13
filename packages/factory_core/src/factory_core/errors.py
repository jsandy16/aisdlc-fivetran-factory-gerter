"""Error taxonomy shared by every engine. Each class maps to one exit code and one alert route.

Messages never contain data values or secrets (the pipeline-factory 0.1.0 convention).
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Final


class ErrorClass(StrEnum):
    TRANSIENT = "transient"
    DATA = "data"
    CONFIG = "config"
    SECURITY = "security"
    INTERNAL = "internal"


EXIT_OK: Final = 0
EXIT_CODE: Final[dict[ErrorClass, int]] = {
    ErrorClass.TRANSIENT: 75,  # EX_TEMPFAIL: retried by the task policy
    ErrorClass.DATA: 65,  # EX_DATAERR: fail, no retry, data owner alerted
    ErrorClass.CONFIG: 78,  # EX_CONFIG: fail, no retry, platform / data engineer alerted
    ErrorClass.SECURITY: 77,  # EX_NOPERM: fail, no retry, security paged
    ErrorClass.INTERNAL: 70,  # EX_SOFTWARE: a bug; fail, no retry, platform alerted
}
RETRYABLE: Final = frozenset({ErrorClass.TRANSIENT})


class FactoryError(Exception):
    """A classified failure with a stable machine code and an optional plain-English hint."""

    error_class: ClassVar[ErrorClass] = ErrorClass.INTERNAL

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    @property
    def exit_code(self) -> int:
        return EXIT_CODE[self.error_class]


class TransientError(FactoryError):
    error_class = ErrorClass.TRANSIENT


class DataError(FactoryError):
    error_class = ErrorClass.DATA


class ConfigError(FactoryError):
    error_class = ErrorClass.CONFIG


class SecurityError(FactoryError):
    error_class = ErrorClass.SECURITY


def exit_code_for(exc: BaseException | None) -> int:
    """0 for success; the class's code for a FactoryError; EX_SOFTWARE for anything unexpected."""
    if exc is None:
        return EXIT_OK
    if isinstance(exc, FactoryError):
        return exc.exit_code
    return EXIT_CODE[ErrorClass.INTERNAL]
