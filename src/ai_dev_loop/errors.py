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


class CursorUsageLimitError(AiDevLoopError):
    """Typed handoff after a durable cursor_usage_limit source failure.

    Raised only after the source run has been marked failed and locks released
    so the CLI can offer interactive recovery without holding workflow locks.
    """

    exit_code = EXIT_GENERAL_ERROR
    failure_code = "cursor_usage_limit"

    def __init__(self, message: str, *, run_id: str) -> None:
        super().__init__(message)
        self.run_id = run_id


class AdjudicationSchemaIncompatibleError(AiDevLoopError):
    """Codex rejected the GitHub adjudication output schema structurally.

    Safe for classification and recovery. Must not embed raw JSONL, stderr,
    review Markdown, or thread bodies.
    """

    exit_code = EXIT_GENERAL_ERROR
    failure_code = "adjudication_schema_incompatible"
