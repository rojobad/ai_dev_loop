"""User-facing errors and exit codes."""

from __future__ import annotations

EXIT_SUCCESS = 0
EXIT_GENERAL_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_NOT_IMPLEMENTED = 3
EXIT_VALIDATION_ERROR = 4


class AiDevLoopError(Exception):
    """Base error with a stable exit code."""

    exit_code: int = EXIT_GENERAL_ERROR

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class UsageError(AiDevLoopError):
    exit_code = EXIT_USAGE_ERROR


class ValidationError(AiDevLoopError):
    exit_code = EXIT_VALIDATION_ERROR


class NotImplementedCommandError(AiDevLoopError):
    exit_code = EXIT_NOT_IMPLEMENTED


class LockError(AiDevLoopError):
    exit_code = EXIT_GENERAL_ERROR
