"""Agent process backend protocol for scheduler attempt execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class UnitLifecycleState(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    MISSING = "missing"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class TerminationClass(StrEnum):
    SUCCESS = "success"
    NONZERO_EXIT = "nonzero_exit"
    TIMEOUT = "timeout"
    KILLED = "killed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LaunchRequest:
    attempt_id: str
    unit_identity: str
    working_directory: Path
    agent_argv: list[str]
    lock_path: Path
    stdout_path: Path
    stderr_path: Path
    result_envelope_path: Path


@dataclass(frozen=True)
class ObserveResult:
    lifecycle_state: UnitLifecycleState
    owned: bool
    absence_proven: bool = False
    exit_code: int | None = None
    termination_class: TerminationClass | None = None
    result_envelope_path: Path | None = None


class AgentProcessBackend(Protocol):
    def launch(self, request: LaunchRequest) -> None:
        """Start or adopt the owned transient unit for one attempt."""

    def observe(self, *, unit_identity: str, attempt_id: str) -> ObserveResult:
        """Return authoritative unit lifecycle and exit evidence."""

    def terminate(self, *, unit_identity: str, attempt_id: str) -> None:
        """Stop the owned unit when termination is required."""
