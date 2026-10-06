"""Publish one same-chat successor for a failed standalone Cursor correction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from pydantic import ValidationError

from ai_dev_loop.scheduler.application.cursor_budget_carry import build_budget_carry
from ai_dev_loop.scheduler.application.cursor_evidence import CursorEvidenceError
from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
    MAX_RECOVERY_ARTIFACT_BYTES,
    CursorInitialRecoveryResult,
    CursorInitialRecoveryService,
    _conflict,
    _corrupt,
    _invalid,
    _project_name,
    _publish_fresh_reviewer_input,
    authenticate_required_initial_ancestors,
)
from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
    CursorRecoveryAnalysisResult,
    CursorRecoveryEvidenceBundle,
    _authenticate_failed_cursor_turn,
)
from ai_dev_loop.scheduler.application.recovery_ancestry import (
    RecoveryAncestryError,
    recovery_ancestor_edges,
)
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RUN_CURSOR_TURN_EFFECT_ID,
    RUN_CURSOR_TURN_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.cursor_initial_recovery import (
    EFFECTIVE_PROMPT_REL,
    effective_prompt_bytes,
    recovery_key_for,
)
from ai_dev_loop.scheduler.domain.cursor_recovery_v2 import (
    CORRECTION_BUDGET_CARRY_REL,
    CORRECTION_RECORD_REL,
    AncestorEdgeV2,
    ArtifactBindingV2,
    BoundReviewerEvidenceV2,
    BudgetCarryV1,
    CursorRecoveryPublicationIntentV2,
    CursorRecoveryRecordV2,
)
from ai_dev_loop.scheduler.domain.events import (
    CodexReviewerBoundEvent,
    CursorChatCreatedEvent,
    CursorInitialRecoverySuccessorCreatedEvent,
    WorktreeAdmittedEvent,
)
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    BlockedState,
    CodexWorkflowCheckpoint,
    CursorReadyState,
    CursorWorkflowCheckpoint,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactError
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_SERVICE = CursorInitialRecoveryService


def force_correction(
    service: _SERVICE,
    run_id: str,
    analysis: CursorRecoveryAnalysisResult,
) -> CursorInitialRecoveryResult:
    evidence = analysis.evidence
    if evidence is None or evidence.sequence_id is not None:
        raise _invalid("forced Cursor recovery does not support sequence runs")
    if evidence.turn_kind != "correction":
        raise _invalid("forced Cursor recovery does not support this turn")
    _require_correction_fields(evidence)
    authenticate_required_initial_ancestors(service.store, service.artifacts, run_id)
    now = service._now_factory()
    key = recovery_key_for(run_id, evidence.failed_attempt_id)
    with service.store.begin_read() as conn:
        existing = service.store.get_cursor_initial_recovery_by_key(conn, recovery_key=key)
    if existing is None:
        if service._before_create is not None:
            service._before_create()
        existing = _create_candidate(service, run_id, evidence, key, now)
        service._raise_fault("after_candidate")
    return service._resume(existing)


def resume_correction_publication(
    service: _SERVICE,
    row: sqlite3.Row,
) -> CursorInitialRecoveryResult:
    intent = _verified_intent(row)
    status = str(row["status"])
    if status == "cancelled":
        raise _conflict("correction recovery publication was cancelled")
    if status == "ready":
        _verified_record(service, row)
        return _result(intent, changed=False, replay=True, status="ready")
    if _successor_cancelled(service, intent.successor_run_id):
        _mark_cancelled(service, row, intent)
        raise _conflict("correction recovery successor was aborted before publication")
    _publish_files(service, intent)
    service._raise_fault("after_artifacts")
    ready = _mark_ready(service, row, intent)
    service._raise_fault("after_ready")
    return ready


def _require_correction_fields(evidence: CursorRecoveryEvidenceBundle) -> None:
    required = (
        evidence.fix_prompt_path,
        evidence.fix_prompt_sha256,
        evidence.fix_owner_run_id,
        evidence.staged_patch_path,
        evidence.staged_patch_sha256,
        evidence.staged_owner_run_id,
        evidence.review_result_path,
        evidence.review_result_sha256,
        evidence.review_result_owner_run_id,
        evidence.reviewer_session_id,
        evidence.reviewer_binding_run_id,
        evidence.reviewer_bootstrap_run_id,
        evidence.binding_artifact_path,
        evidence.binding_artifact_sha256,
        evidence.bootstrap_attempt_id,
        evidence.bootstrap_events_path,
        evidence.bootstrap_events_sha256,
        evidence.admitted_artifact_path,
        evidence.admitted_artifact_sha256,
        evidence.admitted_owner_run_id,
    )
    if any(value is None or value == "" for value in required):
        raise _invalid("forced correction recovery is missing authenticated evidence")
    if evidence.reviews_completed < 1:
        raise _invalid("forced correction recovery requires a completed review")
    if evidence.effective_review_ceiling <= evidence.reviews_completed:
        raise _invalid("forced correction recovery has no remaining review budget")


def _create_candidate(
    service: _SERVICE,
    source_run_id: str,
    evidence: CursorRecoveryEvidenceBundle,
    recovery_key: str,
    now: datetime,
) -> sqlite3.Row:
    successor_run_id = service._run_id_factory(_project_name(service.store, source_run_id), now)
    service._require_current_source_authority(
        source_run_id,
        failed_attempt_id=evidence.failed_attempt_id,
        dispatch_id=evidence.dispatch_id,
        allowed_reservation_owners={source_run_id},
    )
    parent = _prompt_parent(service, evidence)
    base_owner, base_path, base_sha = _base_prompt(service, evidence, parent)
    assert evidence.reviewer_session_id is not None
    assert evidence.fix_prompt_path is not None
    assert evidence.staged_patch_path is not None
    assert evidence.review_result_path is not None
    assert evidence.admitted_artifact_path is not None
    intent = CursorRecoveryPublicationIntentV2(
        schema_version=2,
        turn_kind="correction",
        recovery_key=recovery_key,
        source_run_id=source_run_id,
        successor_run_id=successor_run_id,
        failed_attempt_id=evidence.failed_attempt_id,
        dispatch_id=evidence.dispatch_id,
        status="pending",
        parent_recovery_key=None if parent is None else parent[0],
        record_sha256=None,
        budget_carry_sha256=None,
        base_prompt_path=base_path,
        base_prompt_sha256=base_sha,
        base_prompt_owner_run_id=base_owner,
        chat_id=evidence.chat_id,
        chat_owner_run_id=evidence.chat_owner_run_id,
        chat_artifact_path=evidence.chat_artifact_path,
        chat_artifact_sha256=evidence.chat_artifact_sha256,
        iteration=evidence.iteration,
        admitted_artifact_path=evidence.admitted_artifact_path,
        admitted_artifact_sha256=evidence.admitted_artifact_sha256 or "",
        admitted_owner_run_id=evidence.admitted_owner_run_id or source_run_id,
        plan_sha256=_context_sha(service.store, source_run_id, "plan"),
        submitted_prompt_sha256=_context_sha(service.store, source_run_id, "prompt"),
        config_sha256=_context_sha(service.store, source_run_id, "config"),
        reviews_completed=evidence.reviews_completed,
        submitted_max_review_iterations=evidence.submitted_max_review_iterations,
        effective_review_ceiling=evidence.effective_review_ceiling,
        reviewer_session_id=evidence.reviewer_session_id,
        bootstrap_run_id=evidence.reviewer_bootstrap_run_id or "",
        bootstrap_attempt_id=evidence.bootstrap_attempt_id or "",
        bootstrap_events_path=evidence.bootstrap_events_path or "",
        bootstrap_events_sha256=evidence.bootstrap_events_sha256 or "",
        binding_run_id=evidence.reviewer_binding_run_id or "",
        binding_artifact_path=evidence.binding_artifact_path or "",
        binding_artifact_sha256=evidence.binding_artifact_sha256 or "",
        fix_owner_run_id=evidence.fix_owner_run_id or "",
        fix_prompt_path=evidence.fix_prompt_path,
        fix_prompt_sha256=evidence.fix_prompt_sha256 or "",
        staged_owner_run_id=evidence.staged_owner_run_id or "",
        staged_patch_path=evidence.staged_patch_path,
        staged_patch_sha256=evidence.staged_patch_sha256 or "",
        review_owner_run_id=evidence.review_result_owner_run_id or "",
        review_result_path=evidence.review_result_path,
        review_result_sha256=evidence.review_result_sha256 or "",
    )
    intent_bytes = intent.canonical_bytes()
    try:
        return _insert_candidate(
            service,
            evidence,
            recovery_key,
            now,
            intent,
            intent_bytes,
            parent,
        )
    except sqlite3.IntegrityError:
        with service.store.begin_read() as conn:
            current = service.store.get_cursor_initial_recovery_by_key(
                conn,
                recovery_key=recovery_key,
            )
        if current is None:
            raise
        return current


def _insert_candidate(
    service: _SERVICE,
    evidence: CursorRecoveryEvidenceBundle,
    recovery_key: str,
    now: datetime,
    intent: CursorRecoveryPublicationIntentV2,
    intent_bytes: bytes,
    parent: tuple[str, CursorRecoveryRecordV2] | None,
) -> sqlite3.Row:
    with service.store.begin_immediate() as conn:
        current = service.store.get_cursor_initial_recovery_by_key(
            conn,
            recovery_key=recovery_key,
        )
        if current is not None:
            return current
        service._require_current_source_authority(
            intent.source_run_id,
            failed_attempt_id=intent.failed_attempt_id,
            dispatch_id=intent.dispatch_id,
            allowed_reservation_owners={intent.source_run_id},
        )
        service._guard_publication_ledger(
            conn,
            source_run_id=intent.source_run_id,
            failed_attempt_id=intent.failed_attempt_id,
            allowed_reservation_owners={intent.source_run_id},
        )
        state, _, _ = service.store.load_validated_snapshot(conn, intent.source_run_id)
        if not isinstance(state, BlockedState) or state.block_reason_kind != "cursor_failure":
            raise _conflict("source run changed before correction recovery publication")
        if state.context.sequence is not None:
            raise _invalid("forced Cursor recovery does not support sequence runs")
        successor = _successor_state(
            source=state,
            successor_run_id=intent.successor_run_id,
            intent=intent,
            now=now,
        )
        _insert_successor(service, conn, successor, intent, evidence, recovery_key, now)
        claimed = service.store.claim_reservation_for_successor(
            conn,
            successor_run_id=intent.successor_run_id,
            source_run_id=intent.source_run_id,
            worktree_key=state.context.repository.worktree_key,
            repository_root=state.context.repository.root,
            now=now,
        )
        if not claimed:
            raise _conflict("repository reservation is held by another run")
        inserted = service.store.insert_cursor_initial_recovery(
            conn,
            recovery_key=recovery_key,
            source_run_id=intent.source_run_id,
            failed_attempt_id=intent.failed_attempt_id,
            successor_run_id=intent.successor_run_id,
            dispatch_id=intent.dispatch_id,
            parent_recovery_key=None if parent is None else parent[0],
            intent_payload=intent_bytes.decode("utf-8"),
            intent_payload_sha256=hashlib.sha256(intent_bytes).hexdigest(),
            now=now,
        )
        if not inserted:
            current = service.store.get_cursor_initial_recovery_by_key(
                conn,
                recovery_key=recovery_key,
            )
            if current is None:
                raise _conflict("correction recovery relation was not recorded")
            return current
        row = service.store.get_cursor_initial_recovery_by_key(conn, recovery_key=recovery_key)
    if row is None:
        raise _conflict("correction recovery relation disappeared after insert")
    return row


def _insert_successor(
    service: _SERVICE,
    conn: sqlite3.Connection,
    successor: CursorReadyState,
    intent: CursorRecoveryPublicationIntentV2,
    evidence: CursorRecoveryEvidenceBundle,
    recovery_key: str,
    now: datetime,
) -> None:
    kind, payload, digest = service.store.dump_state(successor)
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    conn.execute(
        """
        INSERT INTO scheduler_runs(
            run_id, state_kind, state_payload, state_payload_sha256,
            version, idempotency_key, worktree_key, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            successor.run_id,
            kind,
            payload,
            digest,
            successor.version,
            successor.idempotency_key,
            successor.context.repository.worktree_key,
            now_text,
            now_text,
        ),
    )
    created = CursorInitialRecoverySuccessorCreatedEvent(
        source_run_id=evidence.run_id,
        successor_run_id=successor.run_id,
        recovery_key=recovery_key,
        failed_attempt_id=evidence.failed_attempt_id,
        iteration=evidence.iteration,
    )
    admitted = WorktreeAdmittedEvent(
        run_id=successor.run_id,
        admission_status_artifact_path=intent.admitted_artifact_path,
        admission_status_sha256=intent.admitted_artifact_sha256,
        resolved_root=successor.context.repository.root,
    )
    chat = CursorChatCreatedEvent(
        run_id=successor.run_id,
        chat_id=intent.chat_id,
        chat_artifact_path=intent.chat_artifact_path,
        chat_artifact_sha256=intent.chat_artifact_sha256,
    )
    bound = CodexReviewerBoundEvent(
        run_id=successor.run_id,
        reviewer_session_id_prefix=intent.reviewer_session_id[:8],
        binding_artifact_path=intent.binding_artifact_path,
        binding_artifact_sha256=intent.binding_artifact_sha256,
    )
    for event in (created, admitted, chat, bound):
        service.store.append_event(
            conn,
            event_id=service._event_id_factory(),
            run_id=successor.run_id,
            sequence=service.store.next_event_sequence(conn, successor.run_id),
            event=event,
            now=now,
        )


def _successor_state(
    *,
    source: BlockedState,
    successor_run_id: str,
    intent: CursorRecoveryPublicationIntentV2,
    now: datetime,
) -> CursorReadyState:
    import secrets

    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return CursorReadyState(
        run_id=successor_run_id,
        version=1,
        submitted_at=now_text,
        updated_at=now_text,
        idempotency_key=secrets.token_hex(32),
        context=source.context,
        checkpoint=AdmittedRunCheckpoint(
            authorized_at=source.authorized_at or source.blocked_at,
            authorized_controller_session_id=source.authorized_controller_session_id,
            admitted_at=source.blocked_at,
            admission_status_artifact_path=intent.admitted_artifact_path,
            admission_status_sha256=intent.admitted_artifact_sha256,
        ),
        cursor=CursorWorkflowCheckpoint(
            iteration=intent.iteration,
            chat_id=intent.chat_id,
            chat_artifact_path=intent.chat_artifact_path,
            chat_artifact_sha256=intent.chat_artifact_sha256,
            original_prompt_path=source.context.plan_prompt.prompt_artifact_path,
            original_prompt_sha256=source.context.plan_prompt.prompt_sha256,
        ),
        codex=CodexWorkflowCheckpoint(
            review_iteration=intent.reviews_completed,
            reviews_completed=intent.reviews_completed,
            reviewer_session_id=intent.reviewer_session_id,
            binding_artifact_path=intent.binding_artifact_path,
            binding_artifact_sha256=intent.binding_artifact_sha256,
            latest_fix_prompt_path=intent.fix_prompt_path,
            latest_fix_prompt_sha256=intent.fix_prompt_sha256,
            latest_correction_envelope_path=intent.base_prompt_path,
            latest_correction_envelope_sha256=intent.base_prompt_sha256,
            latest_review_result_path=intent.review_result_path,
            latest_review_result_sha256=intent.review_result_sha256,
        ),
    )


def _publish_files(
    service: _SERVICE,
    intent: CursorRecoveryPublicationIntentV2,
) -> CursorRecoveryRecordV2:
    service._require_current_source_authority(
        intent.source_run_id,
        failed_attempt_id=intent.failed_attempt_id,
        dispatch_id=intent.dispatch_id,
        allowed_reservation_owners={intent.source_run_id, intent.successor_run_id},
    )
    parent = _verified_prompt_parent(service, intent)
    _copy_inputs(service, intent)
    base = service.artifacts.read_verified_bytes(
        intent.base_prompt_owner_run_id,
        intent.base_prompt_path,
        expected_sha256=intent.base_prompt_sha256,
    )
    service.artifacts.publish_or_verify_bytes(
        intent.successor_run_id,
        intent.base_prompt_path,
        base,
        max_bytes=MAX_RECOVERY_ARTIFACT_BYTES,
    )
    effective = effective_prompt_bytes(base)
    effective_sha = hashlib.sha256(effective).hexdigest()
    service.artifacts.publish_or_verify_bytes(
        intent.successor_run_id,
        EFFECTIVE_PROMPT_REL,
        effective,
        max_bytes=MAX_RECOVERY_ARTIFACT_BYTES,
    )
    with service.store.begin_read() as conn:
        carry = build_budget_carry(
            service.store,
            service.artifacts,
            conn,
            source_run_id=intent.source_run_id,
            successor_run_id=intent.successor_run_id,
            recovery_key=intent.recovery_key,
        )
    if (
        carry.inherited_reviews_completed != intent.reviews_completed
        or carry.inherited_effective_ceiling != intent.effective_review_ceiling
        or carry.submitted_max_review_iterations != intent.submitted_max_review_iterations
    ):
        raise _corrupt("budget carry disagrees with authenticated correction evidence")
    service.artifacts.publish_or_verify_bytes(
        intent.successor_run_id,
        CORRECTION_BUDGET_CARRY_REL,
        carry.canonical_bytes(),
        max_bytes=MAX_RECOVERY_ARTIFACT_BYTES,
    )
    ancestors = _ancestor_models(service.store, intent.source_run_id)
    record = CursorRecoveryRecordV2(
        schema_version=2,
        turn_kind="correction",
        reviewer=BoundReviewerEvidenceV2(
            form="bound",
            session_id=intent.reviewer_session_id,
            bootstrap_run_id=intent.bootstrap_run_id,
            bootstrap_attempt_id=intent.bootstrap_attempt_id,
            bootstrap_events_path=intent.bootstrap_events_path,
            bootstrap_events_sha256=intent.bootstrap_events_sha256,
            binding_run_id=intent.binding_run_id,
            binding_artifact_path=intent.binding_artifact_path,
            binding_artifact_sha256=intent.binding_artifact_sha256,
        ),
        reviews_completed=intent.reviews_completed,
        submitted_max_review_iterations=intent.submitted_max_review_iterations,
        effective_review_ceiling=intent.effective_review_ceiling,
        source_run_id=intent.source_run_id,
        successor_run_id=intent.successor_run_id,
        failed_attempt_id=intent.failed_attempt_id,
        dispatch_id=intent.dispatch_id,
        chat_id=intent.chat_id,
        chat_owner_run_id=intent.chat_owner_run_id,
        iteration=intent.iteration,
        admitted=ArtifactBindingV2(
            owner_run_id=intent.admitted_owner_run_id,
            relative_path=intent.admitted_artifact_path,
            sha256=intent.admitted_artifact_sha256,
        ),
        plan_sha256=intent.plan_sha256,
        submitted_prompt_sha256=intent.submitted_prompt_sha256,
        config_sha256=intent.config_sha256,
        base_prompt=ArtifactBindingV2(
            owner_run_id=intent.base_prompt_owner_run_id,
            relative_path=intent.base_prompt_path,
            sha256=intent.base_prompt_sha256,
        ),
        effective_prompt_path=EFFECTIVE_PROMPT_REL,
        effective_prompt_sha256=effective_sha,
        raw_fix=ArtifactBindingV2(
            owner_run_id=intent.fix_owner_run_id,
            relative_path=intent.fix_prompt_path,
            sha256=intent.fix_prompt_sha256,
        ),
        staged_patch=ArtifactBindingV2(
            owner_run_id=intent.staged_owner_run_id,
            relative_path=intent.staged_patch_path,
            sha256=intent.staged_patch_sha256,
        ),
        review_result=ArtifactBindingV2(
            owner_run_id=intent.review_owner_run_id,
            relative_path=intent.review_result_path,
            sha256=intent.review_result_sha256,
        ),
        parent_source_run_id=None if parent is None else parent.source_run_id,
        parent_recovery_key=None if parent is None else intent.parent_recovery_key,
        parent_record_sha256=None if parent is None else parent.canonical_sha256(),
        budget_carry_path=CORRECTION_BUDGET_CARRY_REL,
        budget_carry_sha256=carry.canonical_sha256(),
        ancestors=ancestors,
    )
    service.artifacts.publish_or_verify_bytes(
        intent.successor_run_id,
        CORRECTION_RECORD_REL,
        record.canonical_bytes(),
        max_bytes=MAX_RECOVERY_ARTIFACT_BYTES,
    )
    return record


def _copy_inputs(service: _SERVICE, intent: CursorRecoveryPublicationIntentV2) -> None:
    with service.store.begin_read() as conn:
        state, _, _ = service.store.load_validated_snapshot(conn, intent.source_run_id)
    context = state.context
    copies = [
        (intent.source_run_id, context.plan_prompt.plan_artifact_path, context.plan_prompt.plan_sha256),
        (
            intent.source_run_id,
            context.plan_prompt.prompt_artifact_path,
            context.plan_prompt.prompt_sha256,
        ),
        (
            intent.source_run_id,
            context.effective_config.effective_config_artifact_path,
            context.effective_config.effective_config_sha256,
        ),
        (
            intent.source_run_id,
            context.effective_config.source_config_artifact_path,
            context.effective_config.source_config_sha256,
        ),
        (intent.admitted_owner_run_id, intent.admitted_artifact_path, intent.admitted_artifact_sha256),
        (intent.chat_owner_run_id, intent.chat_artifact_path, intent.chat_artifact_sha256),
        (intent.binding_run_id, intent.binding_artifact_path, intent.binding_artifact_sha256),
        (intent.fix_owner_run_id, intent.fix_prompt_path, intent.fix_prompt_sha256),
        (intent.staged_owner_run_id, intent.staged_patch_path, intent.staged_patch_sha256),
        (intent.review_owner_run_id, intent.review_result_path, intent.review_result_sha256),
        (intent.bootstrap_run_id, intent.bootstrap_events_path, intent.bootstrap_events_sha256),
    ]
    if context.baseline_status_artifact_path and context.baseline_status_sha256:
        copies.append(
            (
                intent.source_run_id,
                context.baseline_status_artifact_path,
                context.baseline_status_sha256,
            )
        )
    for owner, relative_path, digest in copies:
        payload = service.artifacts.read_verified_bytes(
            owner,
            relative_path,
            expected_sha256=digest,
        )
        service.artifacts.publish_or_verify_bytes(
            intent.successor_run_id,
            relative_path,
            payload,
            max_bytes=MAX_RECOVERY_ARTIFACT_BYTES,
        )
    _publish_fresh_reviewer_input(
        service.store,
        service.artifacts,
        successor_run_id=intent.successor_run_id,
        start_run_id=intent.source_run_id,
        context=context,
    )


def _mark_ready(
    service: _SERVICE,
    row: sqlite3.Row,
    intent: CursorRecoveryPublicationIntentV2,
) -> CursorInitialRecoveryResult:
    now = service._now_factory()
    if service._before_ready_commit is not None:
        service._before_ready_commit()
    with service.store.begin_immediate() as conn:
        service._require_current_source_authority(
            intent.source_run_id,
            failed_attempt_id=intent.failed_attempt_id,
            dispatch_id=intent.dispatch_id,
            allowed_reservation_owners={intent.source_run_id, intent.successor_run_id},
        )
        record = _read_record(service, intent.successor_run_id)
        carry = _read_carry(service, intent.successor_run_id, record)
        _assert_record_matches_intent(record, intent, carry)
        expected = build_budget_carry(
            service.store,
            service.artifacts,
            conn,
            source_run_id=intent.source_run_id,
            successor_run_id=intent.successor_run_id,
            recovery_key=intent.recovery_key,
        )
        if carry.canonical_bytes() != expected.canonical_bytes():
            raise _corrupt("budget carry does not match authenticated extension authority")
        record_bytes = record.canonical_bytes()
        record_sha = hashlib.sha256(record_bytes).hexdigest()
        ready_intent = intent.model_copy(
            update={
                "status": "ready",
                "record_sha256": record_sha,
                "budget_carry_sha256": carry.canonical_sha256(),
            }
        )
        ready_bytes = ready_intent.canonical_bytes()
        current = service.store.get_cursor_initial_recovery_by_key(
            conn,
            recovery_key=intent.recovery_key,
        )
        if current is None:
            raise _corrupt("correction recovery relation disappeared")
        if str(current["status"]) == "ready":
            _verified_record(service, current)
            return _result(ready_intent, changed=False, replay=True, status="ready")
        if str(current["status"]) != "pending":
            raise _conflict("correction recovery publication was cancelled")
        if service.store.has_abort_requested_for_run(conn, intent.successor_run_id):
            raise _conflict("correction recovery successor was aborted")
        service._guard_publication_ledger(
            conn,
            source_run_id=intent.source_run_id,
            failed_attempt_id=intent.failed_attempt_id,
            allowed_reservation_owners={intent.successor_run_id},
        )
        state, version, _ = service.store.load_validated_snapshot(conn, intent.successor_run_id)
        if not isinstance(state, CursorReadyState):
            raise _conflict("correction recovery successor is no longer waiting to publish")
        if (
            state.codex.reviewer_session_id != intent.reviewer_session_id
            or state.codex.reviews_completed != intent.reviews_completed
            or state.context.workflow.max_review_iterations != intent.submitted_max_review_iterations
        ):
            raise _corrupt("correction recovery successor lost reviewer or budget continuity")
        updated = state.model_copy(
            update={
                "version": version + 1,
                "updated_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "cursor": state.cursor.model_copy(
                    update={
                        "continuation_envelope_path": record.effective_prompt_path,
                        "continuation_envelope_sha256": record.effective_prompt_sha256,
                        "chat_id": record.chat_id,
                        "iteration": record.iteration,
                    }
                ),
            }
        )
        if not service.store.compare_and_swap_state(
            conn,
            run_id=intent.successor_run_id,
            expected_version=version,
            new_state=updated,
            now=now,
        ):
            raise _conflict("correction recovery successor changed during publication")
        changed = service.store.update_cursor_initial_recovery_status(
            conn,
            recovery_key=intent.recovery_key,
            expected_status="pending",
            status="ready",
            intent_payload=ready_bytes.decode("utf-8"),
            intent_payload_sha256=hashlib.sha256(ready_bytes).hexdigest(),
            record_artifact_path=CORRECTION_RECORD_REL,
            record_sha256=record_sha,
            now=now,
        )
        if not changed:
            raise _conflict("correction recovery publication status changed")
        pending = [
            effect
            for effect in service.store.list_eligible_effects(
                conn,
                run_id=intent.successor_run_id,
                now=now,
            )
            if str(effect["effect_kind"]) == RUN_CURSOR_TURN_EFFECT_KIND
        ]
        if not pending:
            created_event = conn.execute(
                """
                SELECT event_id FROM scheduler_events
                WHERE run_id = ? AND event_kind = ?
                ORDER BY sequence ASC
                LIMIT 1
                """,
                (intent.successor_run_id, "cursor_initial_recovery_successor_created"),
            ).fetchone()
            if created_event is None:
                raise _corrupt("correction recovery successor event is missing")
            service.store.insert_effect(
                conn,
                dispatch_id=service._dispatch_id_factory(),
                source_event_id=str(created_event["event_id"]),
                run_id=intent.successor_run_id,
                effect_id=RUN_CURSOR_TURN_EFFECT_ID,
                effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
                effect_payload={"iteration": record.iteration},
                available_at=now,
                claimed_run_version=updated.version,
                now=now,
            )
    return _result(ready_intent, changed=True, replay=False, status="ready")


def _mark_cancelled(
    service: _SERVICE,
    row: sqlite3.Row,
    intent: CursorRecoveryPublicationIntentV2,
) -> None:
    cancelled = intent.model_copy(
        update={"status": "cancelled", "record_sha256": None, "budget_carry_sha256": None}
    )
    payload = cancelled.canonical_bytes()
    now = service._now_factory()
    with service.store.begin_immediate() as conn:
        service.store.update_cursor_initial_recovery_status(
            conn,
            recovery_key=str(row["recovery_key"]),
            expected_status="pending",
            status="cancelled",
            intent_payload=payload.decode("utf-8"),
            intent_payload_sha256=hashlib.sha256(payload).hexdigest(),
            record_artifact_path=None,
            record_sha256=None,
            now=now,
        )


def _successor_cancelled(service: _SERVICE, successor_run_id: str) -> bool:
    with service.store.begin_read() as conn:
        if service.store.has_abort_requested_for_run(conn, successor_run_id):
            return True
        state, _, _ = service.store.load_validated_snapshot(conn, successor_run_id)
    return state.kind == "aborted"


def _verified_intent(row: sqlite3.Row) -> CursorRecoveryPublicationIntentV2:
    payload = str(row["intent_payload"]).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != str(row["intent_payload_sha256"]):
        raise _corrupt("correction recovery publication intent digest mismatch")
    try:
        intent = CursorRecoveryPublicationIntentV2.model_validate_json(payload)
    except ValidationError as exc:
        raise _corrupt("correction recovery publication intent failed authentication") from exc
    if intent.canonical_bytes() != payload:
        raise _corrupt("correction recovery publication intent is not canonical")
    if (
        intent.recovery_key != str(row["recovery_key"])
        or intent.source_run_id != str(row["source_run_id"])
        or intent.successor_run_id != str(row["successor_run_id"])
        or intent.failed_attempt_id != str(row["failed_attempt_id"])
        or intent.dispatch_id != str(row["dispatch_id"])
        or intent.status != str(row["status"])
    ):
        raise _corrupt("correction recovery publication intent disagrees with the ledger relation")
    stored_record = row["record_sha256"]
    if (intent.record_sha256 or None) != (str(stored_record) if stored_record else None):
        raise _corrupt("correction recovery record digest disagrees with publication intent")
    parent = row["parent_recovery_key"]
    if (intent.parent_recovery_key or None) != (str(parent) if parent else None):
        raise _corrupt("correction recovery parent provenance disagrees with the ledger relation")
    return intent


def _read_record(service: _SERVICE, successor_run_id: str) -> CursorRecoveryRecordV2:
    try:
        payload = (service.artifacts.run_root(successor_run_id) / CORRECTION_RECORD_REL).read_bytes()
        record = CursorRecoveryRecordV2.model_validate_json(payload)
    except (OSError, ValidationError, UnicodeError) as exc:
        raise _corrupt("correction recovery record failed authentication") from exc
    if record.canonical_bytes() != payload:
        raise _corrupt("correction recovery record is not canonical")
    return record


def _read_carry(
    service: _SERVICE,
    successor_run_id: str,
    record: CursorRecoveryRecordV2,
) -> BudgetCarryV1:
    try:
        payload = service.artifacts.read_verified_bytes(
            successor_run_id,
            record.budget_carry_path,
            expected_sha256=record.budget_carry_sha256,
        )
        carry = BudgetCarryV1.model_validate_json(payload)
    except (ProtectedArtifactError, OSError, ValidationError, UnicodeError) as exc:
        raise _corrupt("budget carry failed authentication") from exc
    if carry.canonical_bytes() != payload:
        raise _corrupt("budget carry is not canonical")
    return carry


def _verified_record(service: _SERVICE, row: sqlite3.Row) -> CursorRecoveryRecordV2:
    intent = _verified_intent(row)
    if intent.status != "ready" or not intent.record_sha256 or not intent.budget_carry_sha256:
        raise _corrupt("correction recovery record is not published")
    path = str(row["record_artifact_path"] or "")
    if path != CORRECTION_RECORD_REL:
        raise _corrupt("correction recovery record path is not the v2 artifact")
    try:
        payload = service.artifacts.read_verified_bytes(
            intent.successor_run_id,
            path,
            expected_sha256=intent.record_sha256,
        )
        record = CursorRecoveryRecordV2.model_validate_json(payload)
    except (ProtectedArtifactError, OSError, ValidationError, UnicodeError) as exc:
        raise _corrupt("correction recovery record failed authentication") from exc
    if hashlib.sha256(record.canonical_bytes()).hexdigest() != intent.record_sha256:
        raise _corrupt("correction recovery record bytes do not match the ledger digest")
    authenticate_required_initial_ancestors(
        service.store,
        service.artifacts,
        record.source_run_id,
    )
    carry = _read_carry(service, intent.successor_run_id, record)
    if carry.canonical_sha256() != intent.budget_carry_sha256:
        raise _corrupt("budget carry digest disagrees with publication intent")
    _assert_record_matches_intent(record, intent, carry)
    with service.store.begin_read() as conn:
        expected = build_budget_carry(
            service.store,
            service.artifacts,
            conn,
            source_run_id=record.source_run_id,
            successor_run_id=record.successor_run_id,
            recovery_key=intent.recovery_key,
        )
        edges = _ancestor_models(service.store, record.source_run_id)
    if carry.canonical_bytes() != expected.canonical_bytes():
        raise _corrupt("budget carry does not match authenticated extension authority")
    if record.ancestors != edges:
        raise _corrupt("correction recovery ancestry does not match the ledger")
    _assert_prompt_composition(service, record)
    _assert_historical_owners(service, record)
    evidence = _reauthenticate_bound_reviewer(service, record)
    try:
        _authenticate_failed_cursor_turn(
            service.store,
            service.artifacts,
            evidence_run_id=record.source_run_id,
            attempt=_attempt(service.store, record),
        )
    except (
        CursorEvidenceError,
        ProtectedArtifactError,
        OSError,
        ValidationError,
        ValueError,
    ) as exc:
        raise _corrupt("decisive Cursor failure evidence is no longer authenticated") from exc
    parent: CursorRecoveryRecordV2 | None = None
    if record.parent_recovery_key is not None:
        with service.store.begin_read() as conn:
            parent_row = service.store.get_cursor_initial_recovery_by_key(
                conn,
                recovery_key=record.parent_recovery_key,
            )
        if parent_row is None or str(parent_row["status"]) != "ready":
            raise _corrupt("parent correction recovery record is missing")
        parent = _verified_record(service, parent_row)
        if (
            parent.successor_run_id != record.source_run_id
            or parent.canonical_sha256() != record.parent_record_sha256
            or parent.base_prompt != record.base_prompt
            or parent.chat_id != record.chat_id
        ):
            raise _corrupt("parent correction recovery record does not match the child record")
    _assert_base_prompt_provenance(service, record, evidence, parent)
    return record


def _assert_record_matches_intent(
    record: CursorRecoveryRecordV2,
    intent: CursorRecoveryPublicationIntentV2,
    carry: BudgetCarryV1,
) -> None:
    if (
        record.source_run_id != intent.source_run_id
        or record.successor_run_id != intent.successor_run_id
        or record.failed_attempt_id != intent.failed_attempt_id
        or record.dispatch_id != intent.dispatch_id
        or record.chat_id != intent.chat_id
        or record.iteration != intent.iteration
        or record.reviews_completed != intent.reviews_completed
        or record.submitted_max_review_iterations != intent.submitted_max_review_iterations
        or record.effective_review_ceiling != intent.effective_review_ceiling
        or record.base_prompt.relative_path != intent.base_prompt_path
        or record.base_prompt.sha256 != intent.base_prompt_sha256
        or record.base_prompt.owner_run_id != intent.base_prompt_owner_run_id
        or not isinstance(record.reviewer, BoundReviewerEvidenceV2)
        or record.reviewer.session_id != intent.reviewer_session_id
        or record.reviewer.bootstrap_run_id != intent.bootstrap_run_id
        or record.reviewer.bootstrap_attempt_id != intent.bootstrap_attempt_id
        or carry.inherited_reviews_completed != record.reviews_completed
        or carry.inherited_effective_ceiling != record.effective_review_ceiling
        or carry.submitted_max_review_iterations != record.submitted_max_review_iterations
        or (record.parent_recovery_key or None) != (intent.parent_recovery_key or None)
    ):
        raise _corrupt("correction recovery record disagrees with publication intent")


def _assert_prompt_composition(service: _SERVICE, record: CursorRecoveryRecordV2) -> None:
    try:
        base = service.artifacts.read_verified_bytes(
            record.successor_run_id,
            record.base_prompt.relative_path,
            expected_sha256=record.base_prompt.sha256,
        )
        effective = service.artifacts.read_verified_bytes(
            record.successor_run_id,
            record.effective_prompt_path,
            expected_sha256=record.effective_prompt_sha256,
        )
        assert record.raw_fix is not None
        fix = service.artifacts.read_verified_bytes(
            record.successor_run_id,
            record.raw_fix.relative_path,
            expected_sha256=record.raw_fix.sha256,
        )
    except (ProtectedArtifactError, OSError) as exc:
        raise _corrupt("correction recovery prompt binding failed authentication") from exc
    if effective != effective_prompt_bytes(base):
        raise _corrupt("effective recovery prompt is not the authenticated base plus one note")
    if hashlib.sha256(fix).hexdigest() != record.raw_fix.sha256:
        raise _corrupt("raw fix prompt bytes changed")


def _reauthenticate_bound_reviewer(
    service: _SERVICE,
    record: CursorRecoveryRecordV2,
) -> CursorRecoveryEvidenceBundle:
    """Compare the record with B and causal artifacts from the failed source."""

    from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
        analyze_cursor_recovery_evidence,
        assert_successor_reviewer_continuity,
    )

    if not isinstance(record.reviewer, BoundReviewerEvidenceV2):
        raise _corrupt("correction recovery reviewer is not bound")
    analysis = analyze_cursor_recovery_evidence(
        service.store,
        service.artifacts,
        record.source_run_id,
        reservation_must_match_run=False,
    )
    if analysis.receipt.reason_code == "corrupt_event_page":
        raise _corrupt("event page failed authentication")
    if analysis.receipt.reason_code == "incomplete_event_history":
        raise _corrupt("event history pagination did not complete")
    evidence = analysis.evidence
    if (
        evidence is None
        or not evidence.reviewer_session_id
        or not evidence.reviewer_bootstrap_run_id
        or not evidence.bootstrap_attempt_id
        or not evidence.bootstrap_events_path
        or not evidence.bootstrap_events_sha256
        or not evidence.reviewer_binding_run_id
        or not evidence.binding_artifact_path
        or not evidence.binding_artifact_sha256
    ):
        raise _corrupt("reviewer bootstrap proof is no longer authenticated")
    reviewer = record.reviewer
    if (
        reviewer.session_id != evidence.reviewer_session_id
        or reviewer.bootstrap_run_id != evidence.reviewer_bootstrap_run_id
        or reviewer.bootstrap_attempt_id != evidence.bootstrap_attempt_id
        or reviewer.bootstrap_events_path != evidence.bootstrap_events_path
        or reviewer.bootstrap_events_sha256 != evidence.bootstrap_events_sha256
        or reviewer.binding_run_id != evidence.reviewer_binding_run_id
        or reviewer.binding_artifact_path != evidence.binding_artifact_path
        or reviewer.binding_artifact_sha256 != evidence.binding_artifact_sha256
    ):
        raise _corrupt("recorded reviewer disagrees with the failed source")
    try:
        with service.store.begin_read() as conn:
            assert_successor_reviewer_continuity(
                service.store,
                conn,
                record.successor_run_id,
                evidence,
            )
    except CursorEvidenceError as exc:
        raise _corrupt("successor checkpoint lost reviewer continuity") from exc
    _assert_causal_artifact_owners(record, evidence)
    return evidence


def _assert_causal_artifact_owners(
    record: CursorRecoveryRecordV2,
    evidence: CursorRecoveryEvidenceBundle,
) -> None:
    """Require fix, staging, and review owners to be the causal checkpoint, not a copy."""

    raw_fix = record.raw_fix
    staged_patch = record.staged_patch
    review_result = record.review_result
    if (
        evidence.turn_kind != "correction"
        or raw_fix is None
        or staged_patch is None
        or review_result is None
        or record.failed_attempt_id != evidence.failed_attempt_id
        or record.dispatch_id != evidence.dispatch_id
        or record.iteration != evidence.iteration
        or record.chat_id != evidence.chat_id
        or record.chat_owner_run_id != evidence.chat_owner_run_id
        or record.reviews_completed != evidence.reviews_completed
        or raw_fix.owner_run_id != evidence.fix_owner_run_id
        or raw_fix.relative_path != evidence.fix_prompt_path
        or raw_fix.sha256 != evidence.fix_prompt_sha256
        or staged_patch.owner_run_id != evidence.staged_owner_run_id
        or staged_patch.relative_path != evidence.staged_patch_path
        or staged_patch.sha256 != evidence.staged_patch_sha256
        or review_result.owner_run_id != evidence.review_result_owner_run_id
        or review_result.relative_path != evidence.review_result_path
        or review_result.sha256 != evidence.review_result_sha256
        or record.admitted.owner_run_id != evidence.admitted_owner_run_id
        or record.admitted.relative_path != evidence.admitted_artifact_path
        or record.admitted.sha256 != evidence.admitted_artifact_sha256
    ):
        raise _corrupt("recorded correction evidence owner disagrees with the causal checkpoint")


def _assert_base_prompt_provenance(
    service: _SERVICE,
    record: CursorRecoveryRecordV2,
    evidence: CursorRecoveryEvidenceBundle,
    parent: CursorRecoveryRecordV2 | None,
) -> None:
    """Bind the base prompt to the failed invocation or its canonical recovery parent."""

    matches_invocation = (
        record.base_prompt.owner_run_id == evidence.run_id
        and record.base_prompt.relative_path == evidence.prompt_path
        and record.base_prompt.sha256 == evidence.prompt_sha256
    )
    if matches_invocation:
        return
    if parent is None or record.base_prompt != parent.base_prompt:
        raise _corrupt("recovery base prompt does not match the failed invocation")
    try:
        invocation = service.artifacts.read_verified_bytes(
            evidence.run_id,
            evidence.prompt_path,
            expected_sha256=evidence.prompt_sha256,
        )
        parent_effective = service.artifacts.read_verified_bytes(
            parent.successor_run_id,
            parent.effective_prompt_path,
            expected_sha256=parent.effective_prompt_sha256,
        )
        parent_base = service.artifacts.read_verified_bytes(
            parent.base_prompt.owner_run_id,
            parent.base_prompt.relative_path,
            expected_sha256=parent.base_prompt.sha256,
        )
    except (ProtectedArtifactError, OSError) as exc:
        raise _corrupt("failed invocation prompt failed authentication") from exc
    if parent_effective != effective_prompt_bytes(parent_base) or (
        invocation != parent_effective and parent_effective not in invocation
    ):
        raise _corrupt("failed invocation does not contain the canonical recovery parent")


def _assert_historical_owners(service: _SERVICE, record: CursorRecoveryRecordV2) -> None:
    if not isinstance(record.reviewer, BoundReviewerEvidenceV2):
        raise _corrupt("correction recovery reviewer is not bound")
    bindings = [record.admitted, record.base_prompt]
    if record.raw_fix is not None:
        bindings.append(record.raw_fix)
    if record.staged_patch is not None:
        bindings.append(record.staged_patch)
    if record.review_result is not None:
        bindings.append(record.review_result)
    bindings.append(
        ArtifactBindingV2(
            owner_run_id=record.reviewer.binding_run_id,
            relative_path=record.reviewer.binding_artifact_path,
            sha256=record.reviewer.binding_artifact_sha256,
        )
    )
    assert isinstance(record.reviewer, BoundReviewerEvidenceV2)
    for binding in bindings:
        try:
            service.artifacts.read_verified_bytes(
                binding.owner_run_id,
                binding.relative_path,
                expected_sha256=binding.sha256,
            )
        except (ProtectedArtifactError, OSError) as exc:
            raise _corrupt("correction evidence owner does not authenticate the artifact") from exc


def _attempt(store: SqliteSchedulerStore, record: CursorRecoveryRecordV2) -> sqlite3.Row:
    with store.begin_read() as conn:
        attempt = store.get_attempt_by_id(conn, record.failed_attempt_id)
    if attempt is None or str(attempt["run_id"]) != record.source_run_id:
        raise _corrupt("decisive Cursor failure attempt is missing")
    if str(attempt["dispatch_id"]) != record.dispatch_id:
        raise _corrupt("decisive Cursor failure dispatch does not match the record")
    return attempt


def _ancestor_models(store: SqliteSchedulerStore, source_run_id: str) -> list[AncestorEdgeV2]:
    with store.begin_read() as conn:
        try:
            edges = recovery_ancestor_edges(store, conn, source_run_id)
        except RecoveryAncestryError as exc:
            raise _corrupt(str(exc)) from exc
    return [
        AncestorEdgeV2(
            relation=edge.relation,
            source_run_id=edge.source_run_id,
            successor_run_id=edge.successor_run_id,
            recovery_key=edge.recovery_key,
        )
        for edge in edges
    ]


def _prompt_parent(
    service: _SERVICE,
    evidence: CursorRecoveryEvidenceBundle,
) -> tuple[str, CursorRecoveryRecordV2] | None:
    with service.store.begin_read() as conn:
        row = service.store.get_cursor_initial_recovery_by_successor(
            conn,
            successor_run_id=evidence.run_id,
        )
    if row is None or str(row["status"]) != "ready":
        return None
    try:
        payload = json.loads(str(row["intent_payload"]))
    except json.JSONDecodeError as exc:
        raise _corrupt("parent recovery intent is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        authenticate_required_initial_ancestors(
            service.store,
            service.artifacts,
            evidence.run_id,
        )
        return None
    record = _verified_record(service, row)
    if evidence.chat_id != record.chat_id:
        raise _corrupt("failed successor chat does not match the parent recovery record")
    if (
        evidence.prompt_path == record.effective_prompt_path
        and evidence.prompt_sha256 == record.effective_prompt_sha256
    ):
        return str(row["recovery_key"]), record
    failed = service.artifacts.read_verified_bytes(
        evidence.run_id,
        evidence.prompt_path,
        expected_sha256=evidence.prompt_sha256,
    )
    parent_effective = service.artifacts.read_verified_bytes(
        record.successor_run_id,
        record.effective_prompt_path,
        expected_sha256=record.effective_prompt_sha256,
    )
    parent_base = service.artifacts.read_verified_bytes(
        record.base_prompt.owner_run_id,
        record.base_prompt.relative_path,
        expected_sha256=record.base_prompt.sha256,
    )
    if parent_effective != effective_prompt_bytes(parent_base) or parent_effective not in failed:
        return None
    return str(row["recovery_key"]), record


def _verified_prompt_parent(
    service: _SERVICE,
    intent: CursorRecoveryPublicationIntentV2,
) -> CursorRecoveryRecordV2 | None:
    if intent.parent_recovery_key is None:
        return None
    with service.store.begin_read() as conn:
        row = service.store.get_cursor_initial_recovery_by_key(
            conn,
            recovery_key=intent.parent_recovery_key,
        )
    if row is None or str(row["status"]) != "ready":
        raise _corrupt("parent correction recovery record is missing")
    parent = _verified_record(service, row)
    if (
        parent.base_prompt.relative_path != intent.base_prompt_path
        or parent.base_prompt.sha256 != intent.base_prompt_sha256
        or parent.chat_id != intent.chat_id
    ):
        raise _corrupt("parent correction recovery record does not match publication intent")
    return parent


def _base_prompt(
    service: _SERVICE,
    evidence: CursorRecoveryEvidenceBundle,
    parent: tuple[str, CursorRecoveryRecordV2] | None,
) -> tuple[str, str, str]:
    if parent is None:
        service.artifacts.read_verified_bytes(
            evidence.run_id,
            evidence.prompt_path,
            expected_sha256=evidence.prompt_sha256,
        )
        return evidence.run_id, evidence.prompt_path, evidence.prompt_sha256
    _key, parent_record = parent
    return (
        parent_record.base_prompt.owner_run_id,
        parent_record.base_prompt.relative_path,
        parent_record.base_prompt.sha256,
    )


def _context_sha(store: SqliteSchedulerStore, run_id: str, kind: str) -> str:
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
    if kind == "plan":
        return state.context.plan_prompt.plan_sha256
    if kind == "prompt":
        return state.context.plan_prompt.prompt_sha256
    return state.context.effective_config.effective_config_sha256


def _result(
    intent: CursorRecoveryPublicationIntentV2,
    *,
    changed: bool,
    replay: bool,
    status: str,
) -> CursorInitialRecoveryResult:
    from ai_dev_loop.scheduler.application.contracts import (
        SafeNextAction,
        SafeNextActionKind,
        active_cursor_safe_next_action,
    )

    ready = status == "ready"
    action = (
        active_cursor_safe_next_action()
        if ready
        else SafeNextAction(
            kind=SafeNextActionKind.SCHEDULER_TICK,
            command="ai_dev_loop scheduler tick",
        )
    )
    return CursorInitialRecoveryResult(
        source_run_id=intent.source_run_id,
        successor_run_id=intent.successor_run_id,
        changed=changed,
        idempotent_replay=replay,
        publication_status=status,  # type: ignore[arg-type]
        agent_execution_ready=ready,
        safe_next_action=action,
        turn_kind="correction",
    )


class _LaunchView:
    def __init__(self, store: SqliteSchedulerStore, artifacts: object) -> None:
        self.store = store
        self.artifacts = artifacts


def authenticated_correction_launch(
    store: SqliteSchedulerStore,
    artifacts: object,
    conn: sqlite3.Connection,
    run_id: str,
) -> CursorRecoveryRecordV2 | None:
    """Return the ready v2 record when this run is a correction recovery successor."""

    from ai_dev_loop.scheduler.application.cursor_initial_recovery import _intent_schema_version

    row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=run_id)
    if row is None or _intent_schema_version(row) != 2:
        return None
    if str(row["status"]) != "ready":
        raise _invalid("cursor correction recovery publication is not dispatchable")
    return _verified_record(_LaunchView(store, artifacts), row)  # type: ignore[arg-type]
