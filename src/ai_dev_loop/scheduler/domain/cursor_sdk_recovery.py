"""Versioned Cursor SDK native-run reconciliation and successor authority."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ai_dev_loop.scheduler.domain.common import DomainModel, NonEmptyStr, Sha256Hex

SDK_SUCCESSOR_EFFECTIVE_PROMPT_REL = "prompts/cursor-sdk-recovery/effective.txt"
SDK_SUCCESSOR_BASE_PROMPT_REL = "prompts/cursor-sdk-recovery/base.txt"
SDK_SUCCESSOR_RECORD_REL = "cursor/sdk-recovery/record-v1.json"


class CursorSdkReconciliationIntentV1(DomainModel):
    """Immutable native observation/cancellation authority for one failed turn."""

    schema_version: Literal[1] = 1
    run_id: NonEmptyStr
    source_attempt_id: NonEmptyStr
    source_dispatch_id: NonEmptyStr
    turn_kind: Literal["initial", "correction"]
    iteration: int = Field(ge=1)
    conversation_owner_run_id: NonEmptyStr
    conversation_key: Sha256Hex
    agent_id: NonEmptyStr
    prompt_path: NonEmptyStr
    prompt_sha256: Sha256Hex
    source_journal_path: NonEmptyStr
    source_journal_sha256: Sha256Hex
    identity_artifact_path: NonEmptyStr
    identity_artifact_sha256: Sha256Hex
    source_state: dict[str, object]
    reviews_completed: int = Field(ge=0)
    submitted_max_review_iterations: int = Field(ge=1)
    effective_review_ceiling: int = Field(ge=1)
    native_run_id: NonEmptyStr | None = None
    failure_class: Literal["timeout", "capacity", "failure", "worker_lost", "ambiguous"]
    retry_after_seconds: int | None = Field(default=None, ge=1, le=86_400)

    @field_validator("iteration", "retry_after_seconds", mode="before")
    @classmethod
    def strict_ints(cls, value: object, info: object) -> object:
        if value is None and getattr(info, "field_name", "") == "retry_after_seconds":
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{getattr(info, 'field_name', 'value')} must be a JSON integer")
        return value

    @model_validator(mode="after")
    def capacity_metadata(self) -> CursorSdkReconciliationIntentV1:
        if self.retry_after_seconds is not None and self.failure_class != "capacity":
            raise ValueError("retry_after_seconds requires capacity failure")
        return self

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")


class CursorSdkReconciliationRecordV1(DomainModel):
    """Authenticated result of exact-agent public native-run reconciliation."""

    schema_version: Literal[1] = 1
    run_id: NonEmptyStr
    source_attempt_id: NonEmptyStr
    reconciliation_attempt_id: NonEmptyStr
    source_dispatch_id: NonEmptyStr
    reconciliation_dispatch_id: NonEmptyStr
    conversation_owner_run_id: NonEmptyStr
    conversation_key: Sha256Hex
    agent_id: NonEmptyStr
    prompt_sha256: Sha256Hex
    source_journal_path: NonEmptyStr
    source_journal_sha256: Sha256Hex
    baseline_run_ids: tuple[NonEmptyStr, ...]
    candidate_run_ids: tuple[NonEmptyStr, ...] = ()
    candidate_final_statuses: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)
    native_run_id: NonEmptyStr | None = None
    initial_native_status: NonEmptyStr | None = None
    final_native_status: NonEmptyStr | None = None
    outcome: Literal[
        "terminal_recovered",
        "continuation_ready",
        "ambiguous_missing",
        "ambiguous_multiple",
        "native_unavailable",
        "abort_reconciled",
    ]
    cancellation_requested: bool = False
    terminal_response_path: NonEmptyStr | None = None
    terminal_response_sha256: Sha256Hex | None = None
    cursor_output_fingerprint_path: NonEmptyStr | None = None
    cursor_output_fingerprint_sha256: Sha256Hex | None = None

    @model_validator(mode="after")
    def bindings_agree(self) -> CursorSdkReconciliationRecordV1:
        if len(set(self.baseline_run_ids)) != len(self.baseline_run_ids):
            raise ValueError("baseline run identities must be unique")
        if len(set(self.candidate_run_ids)) != len(self.candidate_run_ids):
            raise ValueError("candidate run identities must be unique")
        if not set(self.candidate_final_statuses).issubset(self.candidate_run_ids):
            raise ValueError("candidate statuses must belong to candidate run identities")
        response = (self.terminal_response_path, self.terminal_response_sha256)
        fingerprint = (
            self.cursor_output_fingerprint_path,
            self.cursor_output_fingerprint_sha256,
        )
        if any(value is None for value in response) != all(value is None for value in response):
            raise ValueError("terminal response path and digest must appear together")
        if any(value is None for value in fingerprint) != all(
            value is None for value in fingerprint
        ):
            raise ValueError("cursor fingerprint path and digest must appear together")
        if self.outcome == "terminal_recovered":
            if self.native_run_id is None or any(value is None for value in response + fingerprint):
                raise ValueError("terminal recovery requires exact result and fingerprint evidence")
        elif any(value is not None for value in response + fingerprint):
            raise ValueError("non-terminal recovery outcomes cannot carry terminal result evidence")
        if self.outcome == "continuation_ready" and self.native_run_id is None:
            raise ValueError("continuation requires an exact terminal native run")
        if self.outcome.startswith("ambiguous_") and self.native_run_id is not None:
            raise ValueError("ambiguous outcomes cannot claim a native run identity")
        if self.outcome == "abort_reconciled" and any(
            value is not None for value in response + fingerprint
        ):
            raise ValueError("abort reconciliation cannot publish a turn result")
        if self.outcome == "abort_reconciled" and set(self.candidate_final_statuses) != set(
            self.candidate_run_ids
        ):
            raise ValueError("abort reconciliation requires every candidate terminal status")
        return self

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")


class CursorSdkReconciliationRecordV2(DomainModel):
    """Reconciliation result whose decision is outside the v1 outcome set.

    Historical v1 bytes stay on ``CursorSdkReconciliationRecordV1``. This model
    is written only for a proved-unsent prompt and lives at ``record-v2.json``.
    """

    schema_version: Literal[2] = 2
    run_id: NonEmptyStr
    source_attempt_id: NonEmptyStr
    reconciliation_attempt_id: NonEmptyStr
    source_dispatch_id: NonEmptyStr
    reconciliation_dispatch_id: NonEmptyStr
    conversation_owner_run_id: NonEmptyStr
    conversation_key: Sha256Hex
    agent_id: NonEmptyStr
    prompt_sha256: Sha256Hex
    source_journal_path: NonEmptyStr
    source_journal_sha256: Sha256Hex
    baseline_run_ids: tuple[NonEmptyStr, ...]
    candidate_run_ids: tuple[NonEmptyStr, ...] = ()
    candidate_final_statuses: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)
    native_run_id: NonEmptyStr | None = None
    initial_native_status: NonEmptyStr | None = None
    final_native_status: NonEmptyStr | None = None
    outcome: Literal[
        "terminal_recovered",
        "continuation_ready",
        "ambiguous_missing",
        "ambiguous_multiple",
        "native_unavailable",
        "abort_reconciled",
        "prompt_unsent",
    ]
    cancellation_requested: bool = False
    terminal_response_path: NonEmptyStr | None = None
    terminal_response_sha256: Sha256Hex | None = None
    cursor_output_fingerprint_path: NonEmptyStr | None = None
    cursor_output_fingerprint_sha256: Sha256Hex | None = None

    @model_validator(mode="after")
    def bindings_agree(self) -> CursorSdkReconciliationRecordV2:
        if len(set(self.baseline_run_ids)) != len(self.baseline_run_ids):
            raise ValueError("baseline run identities must be unique")
        if len(set(self.candidate_run_ids)) != len(self.candidate_run_ids):
            raise ValueError("candidate run identities must be unique")
        if not set(self.candidate_final_statuses).issubset(self.candidate_run_ids):
            raise ValueError("candidate statuses must belong to candidate run identities")
        response = (self.terminal_response_path, self.terminal_response_sha256)
        fingerprint = (
            self.cursor_output_fingerprint_path,
            self.cursor_output_fingerprint_sha256,
        )
        if any(value is None for value in response) != all(value is None for value in response):
            raise ValueError("terminal response path and digest must appear together")
        if any(value is None for value in fingerprint) != all(
            value is None for value in fingerprint
        ):
            raise ValueError("cursor fingerprint path and digest must appear together")
        if self.outcome == "terminal_recovered":
            if self.native_run_id is None or any(value is None for value in response + fingerprint):
                raise ValueError("terminal recovery requires exact result and fingerprint evidence")
        elif any(value is not None for value in response + fingerprint):
            raise ValueError("non-terminal recovery outcomes cannot carry terminal result evidence")
        if self.outcome == "continuation_ready" and self.native_run_id is None:
            raise ValueError("continuation requires an exact terminal native run")
        if self.outcome.startswith("ambiguous_") and self.native_run_id is not None:
            raise ValueError("ambiguous outcomes cannot claim a native run identity")
        if self.outcome == "abort_reconciled" and any(
            value is not None for value in response + fingerprint
        ):
            raise ValueError("abort reconciliation cannot publish a turn result")
        if self.outcome == "abort_reconciled" and set(self.candidate_final_statuses) != set(
            self.candidate_run_ids
        ):
            raise ValueError("abort reconciliation requires every candidate terminal status")
        if self.outcome == "prompt_unsent" and (
            self.native_run_id is not None or self.candidate_run_ids
        ):
            raise ValueError("an unsent prompt cannot claim a native run")
        return self

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")


class CursorSdkSuccessorIntentV1(DomainModel):
    """Copy-once publication authority for an SDK manual recovery successor."""

    schema_version: Literal[3] = 3
    recovery_key: Sha256Hex
    status: Literal["pending", "ready", "cancelled"]
    source_run_id: NonEmptyStr
    successor_run_id: NonEmptyStr
    failed_attempt_id: NonEmptyStr
    dispatch_id: NonEmptyStr
    turn_kind: Literal["initial", "correction"]
    iteration: int = Field(ge=1)
    conversation_owner_run_id: NonEmptyStr
    conversation_key: Sha256Hex
    agent_id: NonEmptyStr
    identity_owner_run_id: NonEmptyStr
    identity_artifact_path: NonEmptyStr
    identity_artifact_sha256: Sha256Hex
    reconciliation_owner_run_id: NonEmptyStr
    reconciliation_artifact_path: NonEmptyStr
    reconciliation_artifact_sha256: Sha256Hex
    base_prompt_owner_run_id: NonEmptyStr
    base_prompt_path: NonEmptyStr
    base_prompt_sha256: Sha256Hex
    admitted_owner_run_id: NonEmptyStr
    admitted_artifact_path: NonEmptyStr
    admitted_artifact_sha256: Sha256Hex
    reviews_completed: int = Field(ge=0)
    submitted_max_review_iterations: int = Field(ge=1)
    effective_review_ceiling: int = Field(ge=1)
    reviewer_session_id: NonEmptyStr | None = None
    reviewer_binding_owner_run_id: NonEmptyStr | None = None
    reviewer_binding_artifact_path: NonEmptyStr | None = None
    reviewer_binding_artifact_sha256: Sha256Hex | None = None
    fix_owner_run_id: NonEmptyStr | None = None
    fix_prompt_path: NonEmptyStr | None = None
    fix_prompt_sha256: Sha256Hex | None = None
    staged_owner_run_id: NonEmptyStr | None = None
    staged_patch_path: NonEmptyStr | None = None
    staged_patch_sha256: Sha256Hex | None = None
    review_owner_run_id: NonEmptyStr | None = None
    review_result_path: NonEmptyStr | None = None
    review_result_sha256: Sha256Hex | None = None
    effective_prompt_path: NonEmptyStr | None = None
    effective_prompt_sha256: Sha256Hex | None = None
    record_sha256: Sha256Hex | None = None

    @model_validator(mode="after")
    def recovery_shape(self) -> CursorSdkSuccessorIntentV1:
        reviewer = (
            self.reviewer_session_id,
            self.reviewer_binding_owner_run_id,
            self.reviewer_binding_artifact_path,
            self.reviewer_binding_artifact_sha256,
        )
        fix = (self.fix_owner_run_id, self.fix_prompt_path, self.fix_prompt_sha256)
        staged = (self.staged_owner_run_id, self.staged_patch_path, self.staged_patch_sha256)
        review = (self.review_owner_run_id, self.review_result_path, self.review_result_sha256)
        if self.turn_kind == "initial":
            if any(value is not None for value in reviewer + fix + staged + review):
                raise ValueError("initial successor cannot carry reviewer correction evidence")
            if self.reviews_completed != 0:
                raise ValueError("initial successor requires zero completed reviews")
        else:
            for group in (reviewer, fix, staged, review):
                if any(value is None for value in group):
                    raise ValueError("correction successor requires complete reviewer evidence")
        if self.effective_review_ceiling <= self.reviews_completed:
            raise ValueError("effective review ceiling must exceed completed reviews")
        ready_fields = (
            self.effective_prompt_path,
            self.effective_prompt_sha256,
            self.record_sha256,
        )
        if self.status == "ready" and any(value is None for value in ready_fields):
            raise ValueError("ready successor requires published prompt and record bindings")
        if self.status != "ready" and any(value is not None for value in ready_fields):
            raise ValueError("unpublished successor cannot claim ready artifacts")
        return self

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")


class CursorSdkSuccessorRecordV1(DomainModel):
    """Private successor record authenticated before dispatch/adoption."""

    schema_version: Literal[1] = 1
    recovery_key: Sha256Hex
    source_run_id: NonEmptyStr
    successor_run_id: NonEmptyStr
    failed_attempt_id: NonEmptyStr
    turn_kind: Literal["initial", "correction"]
    iteration: int = Field(ge=1)
    conversation_owner_run_id: NonEmptyStr
    conversation_key: Sha256Hex
    agent_id: NonEmptyStr
    base_prompt_path: NonEmptyStr
    base_prompt_sha256: Sha256Hex
    effective_prompt_path: NonEmptyStr
    effective_prompt_sha256: Sha256Hex
    reconciliation_artifact_path: NonEmptyStr
    reconciliation_artifact_sha256: Sha256Hex
    reviews_completed: int = Field(ge=0)
    submitted_max_review_iterations: int = Field(ge=1)
    effective_review_ceiling: int = Field(ge=1)
    pending_intent_sha256: Sha256Hex

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
