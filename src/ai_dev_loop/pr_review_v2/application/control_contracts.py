"""Typed control-plane DTOs for the temporary ``pr-review-v2`` public API.

These contracts are privacy-safe: they never embed prompts, patches, thread bodies,
tokens, full opaque identifiers, raw argv, PID/PGID, or environments.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import PositiveInt

from ai_dev_loop.pr_review_v2.application.contracts import AppModel, NextActionCategory
from ai_dev_loop.pr_review_v2.domain.common import (
    NonEmptyId,
    NonEmptyStr,
    SafeActionKind,
    Sha256Hex,
    UtcInstant,
)


class ControlErrorKind(StrEnum):
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    NOT_PREPARED = "not_prepared"
    NOT_RESUMABLE = "not_resumable"
    REQUIRES_USER_CONFIRMATION = "requires_user_confirmation"
    SUPERVISOR = "supervisor"
    ABORT_REFUSED = "abort_refused"
    INTERNAL = "internal"


class ControlError(Exception):
    """Privacy-safe control-plane failure."""

    def __init__(
        self,
        kind: ControlErrorKind,
        safe_message: str,
        *,
        next_action: str | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.kind = kind
        self.safe_message = safe_message
        self.next_action = next_action


class SafeNextAction(StrEnum):
    START = "start"
    RESUME = "resume"
    RESUME_CONFIRM_USER_CONTINUATION = "resume --confirm-user-continuation"
    WAIT_UNTIL = "wait-until"
    INSPECT_PROTECTED_FAILURE = "inspect-protected-failure"
    NONE = "none"


class OriginKind(StrEnum):
    SOURCE_RUN = "source_run"
    EXISTING_PR = "existing_pr"


class PreparedOwnershipKeys(AppModel):
    """Active-ownership keys checked during atomic prepared-run create/reuse."""

    source_run_id: NonEmptyId | None = None
    repository: NonEmptyStr
    pr_number: PositiveInt | None = None
    head_branch: NonEmptyStr | None = None


class PrepareCreateResult(AppModel):
    run_id: NonEmptyId
    origin_kind: OriginKind
    state_kind: Literal["prepared"] = "prepared"
    reused: bool
    repository: NonEmptyStr | None = None
    pr_number: PositiveInt | None = None
    cycle_number: PositiveInt = 1
    next_action: SafeNextAction = SafeNextAction.START
    execution_context_sha256: Sha256Hex


class StartResult(AppModel):
    run_id: NonEmptyId
    state_kind: NonEmptyStr
    transition_applied: bool
    supervisor_action: Literal["spawned", "reused", "repaired", "spawn_failed"]
    next_action: SafeNextAction
    safe_detail: NonEmptyStr | None = None


class ResumeResult(AppModel):
    run_id: NonEmptyId
    state_kind: NonEmptyStr
    transition_applied: bool
    supervisor_action: Literal["spawned", "reused", "repaired", "none", "spawn_failed"]
    next_action: SafeNextAction
    safe_detail: NonEmptyStr | None = None


class AbortProcessAction(StrEnum):
    TERMINATED = "terminated"
    UNNECESSARY = "unnecessary"
    REFUSED = "refused"


class AbortResult(AppModel):
    run_id: NonEmptyId
    state_kind: NonEmptyStr
    abort_persisted: bool
    process_action: AbortProcessAction
    next_action: SafeNextAction = SafeNextAction.NONE
    safe_detail: NonEmptyStr | None = None


class HistoryEntry(AppModel):
    sequence: PositiveInt
    event_kind: NonEmptyStr
    disposition: NonEmptyStr
    created_at: UtcInstant
    effect_kind: NonEmptyStr | None = None
    rejection_code: NonEmptyStr | None = None
    safe_detail: NonEmptyStr | None = None


class ControlStatus(AppModel):
    run_id: NonEmptyId
    origin_kind: OriginKind | None = None
    state_kind: NonEmptyStr
    run_version: PositiveInt
    cycle_number: PositiveInt
    max_external_cycles: PositiveInt | None = None
    max_local_iterations: PositiveInt | None = None
    repository: NonEmptyStr | None = None
    pr_number: PositiveInt | None = None
    head_sha_short: str | None = None
    active_effect_kind: NonEmptyStr | None = None
    effect_status: NonEmptyStr | None = None
    effect_attempt: PositiveInt | None = None
    effect_max_attempts: PositiveInt | None = None
    pending_or_claimed: bool = False
    retry_waiting: bool = False
    next_eligible_at: UtcInstant | None = None
    last_durable_transition_at: UtcInstant | None = None
    last_error_kind: NonEmptyStr | None = None
    last_error_summary: NonEmptyStr | None = None
    resumable: bool = False
    supervisor_live: bool | None = None
    lease_active: bool = False
    safe_action_kind: SafeActionKind | None = None
    safe_action_condition: NonEmptyStr | None = None
    engine_next_action: NextActionCategory
    next_action: SafeNextAction
    recent_history: tuple[HistoryEntry, ...] = ()


class HistoryResult(AppModel):
    run_id: NonEmptyId
    order: Literal["oldest", "newest"]
    limit: PositiveInt
    truncated: bool
    entries: tuple[HistoryEntry, ...]


class OperatorContinuationArtifact(AppModel):
    schema_name: Literal["ai_dev_loop.pr_review_v2.operator_continuation"] = (
        "ai_dev_loop.pr_review_v2.operator_continuation"
    )
    schema_version: Literal[1] = 1
    run_id: NonEmptyId
    repository: NonEmptyStr
    pr_number: PositiveInt
    cycle_number: PositiveInt
    head_sha: NonEmptyStr
    confirmed_at: UtcInstant
    confirmation_kind: Literal["explicit_flag"] = "explicit_flag"


DEFAULT_HISTORY_LIMIT = 50
HARD_HISTORY_MAX = 200

__all__ = [
    "AbortProcessAction",
    "AbortResult",
    "ControlError",
    "ControlErrorKind",
    "ControlStatus",
    "DEFAULT_HISTORY_LIMIT",
    "HARD_HISTORY_MAX",
    "HistoryEntry",
    "HistoryResult",
    "OperatorContinuationArtifact",
    "OriginKind",
    "PrepareCreateResult",
    "PreparedOwnershipKeys",
    "ResumeResult",
    "SafeNextAction",
    "StartResult",
]
