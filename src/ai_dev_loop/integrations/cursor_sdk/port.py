"""Typed, SDK-independent Cursor integration port."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, Literal, Protocol, TypeVar


class CursorSdkOperation(StrEnum):
    OPEN = "open"
    CREATE = "create"
    RESUME = "resume"
    SEND = "send"
    STREAM = "stream"
    GET_RUN = "get_run"
    LIST_RUNS = "list_runs"
    LIST_MESSAGES = "list_messages"
    CANCEL = "cancel"
    PREFLIGHT = "preflight"


class CursorSdkErrorCode(StrEnum):
    SDK_UNAVAILABLE = "sdk_unavailable"
    VERSION_MISMATCH = "version_mismatch"
    INVALID_LOCAL_PATH = "invalid_local_path"
    AUTHENTICATION_FAILED = "authentication_failed"
    RATE_LIMITED = "rate_limited"
    BUSY = "busy"
    FEATURE_UNAVAILABLE = "feature_unavailable"
    UNKNOWN_IDENTITY = "unknown_identity"
    TIMEOUT = "timeout"
    NETWORK = "network"
    INVALID_REQUEST = "invalid_request"
    SERVER_ERROR = "server_error"
    SDK_ERROR = "sdk_error"
    INVALID_STATE = "invalid_state"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True)
class CursorSdkFailure:
    code: CursorSdkErrorCode
    operation: CursorSdkOperation
    agent_id: str | None
    run_id: str | None
    retry_after: str | None = None
    request_id: str | None = None
    evidence_class: Literal["typed", "structured", "message"] | None = None


T = TypeVar("T")


@dataclass(frozen=True)
class CursorSdkOutcome(Generic[T]):
    value: T | None = None
    failure: CursorSdkFailure | None = None

    def __post_init__(self) -> None:
        if (self.value is None) == (self.failure is None):
            raise ValueError("CursorSdkOutcome requires exactly one of value or failure")

    @classmethod
    def success(cls, value: T) -> CursorSdkOutcome[T]:
        return cls(value=value)

    @classmethod
    def failed(cls, failure: CursorSdkFailure) -> CursorSdkOutcome[T]:
        return cls(failure=failure)


@dataclass(frozen=True)
class CursorSdkClientConfig:
    sdk_version: str
    workspace: Path
    state_root: Path
    store_root: Path
    model: str
    parameters: tuple[tuple[str, str], ...]
    credential_ref: str
    setting_sources: tuple[str, ...]
    sandbox: Literal["enabled", "disabled"]
    store_backend: Literal["jsonl", "sqlite"] = "jsonl"
    native_store_layout_version: Literal[1] = 1


@dataclass(frozen=True)
class CursorSdkBridgeHealth:
    sdk_version: str
    bridge_version: str
    protocol_version: str


@dataclass(frozen=True)
class CursorSdkPreflight:
    health: CursorSdkBridgeHealth
    authenticated: bool
    model: str
    parameters: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CursorSdkEnvelope:
    offset: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class CursorSdkAgentIdentity:
    agent_id: str


@dataclass(frozen=True)
class CursorSdkRunIdentity:
    agent_id: str
    run_id: str


@dataclass(frozen=True)
class CursorSdkUsage:
    """Untrusted provider counters retained without coercion.

    Strict numeric validation belongs at the usage-observation boundary.  Keeping
    the SDK values verbatim prevents booleans, strings and fractional values from
    becoming apparently valid counters before that boundary sees them.
    """

    input_tokens: object
    output_tokens: object
    cache_read_tokens: object
    cache_write_tokens: object
    total_tokens: object
    reasoning_tokens: object


@dataclass(frozen=True)
class CursorSdkTerminalRun:
    agent_id: str
    run_id: str
    status: str
    text: str
    usage: CursorSdkUsage | None


@dataclass(frozen=True)
class CursorSdkRunPage:
    runs: tuple[CursorSdkTerminalRun, ...]
    next_cursor: str | None


class CursorSdkPort(Protocol):
    def open(self) -> CursorSdkOutcome[CursorSdkBridgeHealth]: ...

    def close(self) -> None: ...

    def preflight(self) -> CursorSdkOutcome[CursorSdkPreflight]: ...

    def create_agent(self) -> CursorSdkOutcome[CursorSdkAgentIdentity]: ...

    def resume_agent(self, agent_id: str) -> CursorSdkOutcome[CursorSdkAgentIdentity]: ...

    def send(self, agent_id: str, prompt: str) -> CursorSdkOutcome[CursorSdkRunIdentity]: ...

    def stream(
        self,
        agent_id: str,
        run_id: str,
        *,
        mode: Literal["live", "replay"] = "live",
        after_offset: str | None = None,
        on_envelope: Callable[[CursorSdkEnvelope], None] | None = None,
    ) -> CursorSdkOutcome[CursorSdkTerminalRun]: ...

    def get_run(
        self,
        agent_id: str,
        run_id: str,
    ) -> CursorSdkOutcome[CursorSdkTerminalRun]: ...

    def list_runs(
        self,
        agent_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> CursorSdkOutcome[CursorSdkRunPage]: ...

    def list_agent_messages(self, agent_id: str) -> CursorSdkOutcome[int]: ...

    def cancel(self, agent_id: str, run_id: str) -> CursorSdkOutcome[CursorSdkRunIdentity]: ...
