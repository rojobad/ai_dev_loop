"""Integration API error codes and typed failures."""

from __future__ import annotations

from enum import StrEnum


class IntegrationErrorCode(StrEnum):
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED = "UNSUPPORTED"
    DATA_INTEGRITY = "DATA_INTEGRITY"
    IO_ERROR = "IO_ERROR"


_EXIT_BY_CODE: dict[IntegrationErrorCode, int] = {
    IntegrationErrorCode.INTERNAL_ERROR: 1,
    IntegrationErrorCode.INVALID_ARGUMENT: 2,
    IntegrationErrorCode.NOT_FOUND: 3,
    IntegrationErrorCode.UNSUPPORTED: 4,
    IntegrationErrorCode.DATA_INTEGRITY: 5,
    IntegrationErrorCode.IO_ERROR: 6,
}


class IntegrationApiError(Exception):
    """Integration-scoped failure with a stable wire code and exit status."""

    def __init__(
        self,
        code: IntegrationErrorCode,
        message: str,
        *,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code if exit_code is not None else _EXIT_BY_CODE[code]

    @classmethod
    def internal(cls) -> IntegrationApiError:
        return cls(
            IntegrationErrorCode.INTERNAL_ERROR,
            "An internal error occurred.",
        )

    @classmethod
    def invalid_argument(cls, message: str) -> IntegrationApiError:
        return cls(IntegrationErrorCode.INVALID_ARGUMENT, message)

    @classmethod
    def not_found(cls, message: str) -> IntegrationApiError:
        return cls(IntegrationErrorCode.NOT_FOUND, message)

    @classmethod
    def unsupported(cls, message: str) -> IntegrationApiError:
        return cls(IntegrationErrorCode.UNSUPPORTED, message)

    @classmethod
    def data_integrity(cls, message: str) -> IntegrationApiError:
        return cls(IntegrationErrorCode.DATA_INTEGRITY, message)

    @classmethod
    def io_error(cls, message: str) -> IntegrationApiError:
        return cls(IntegrationErrorCode.IO_ERROR, message)
