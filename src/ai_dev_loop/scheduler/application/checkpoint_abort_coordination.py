"""Checkpoint-aware abort coordination for Phase 20.3 sequence handoff."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from ai_dev_loop.runners.git import checkpoint_git_rev_parse
from ai_dev_loop.scheduler.domain.checkpoint import (
    SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT,
    SequenceCheckpointEvidence,
    SequenceCheckpointIntent,
)
from ai_dev_loop.scheduler.domain.state import AbortedState, SchedulerState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON = "checkpoint_cas_authorized"


class CheckpointRefInspectionOutcome(StrEnum):
    """Outcome of comparing live HEAD against checkpoint commit evidence."""

    APPLIED = "applied"
    NOT_APPLIED_AT_PARENT = "not_applied_at_parent"
    NOT_APPLIED_ELSEWHERE = "not_applied_elsewhere"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class CheckpointRefAdvancementInspection:
    outcome: CheckpointRefInspectionOutcome


def inspect_checkpoint_ref_advancement(
    intent: SequenceCheckpointIntent,
    commit_sha256: str | None,
) -> CheckpointRefAdvancementInspection:
    """Classify live HEAD relative to checkpoint commit evidence.

    Failed reads are ambiguous and must not be treated as a verified non-applied CAS.
    """
    if commit_sha256 is None:
        return CheckpointRefAdvancementInspection(
            outcome=CheckpointRefInspectionOutcome.NOT_APPLIED_AT_PARENT,
        )
    repo_root = Path(intent.repository_root)
    try:
        live_head = checkpoint_git_rev_parse(repo_root, "HEAD")
    except Exception:
        return CheckpointRefAdvancementInspection(outcome=CheckpointRefInspectionOutcome.AMBIGUOUS)
    if live_head == commit_sha256:
        return CheckpointRefAdvancementInspection(outcome=CheckpointRefInspectionOutcome.APPLIED)
    if live_head == intent.parent_head:
        return CheckpointRefAdvancementInspection(
            outcome=CheckpointRefInspectionOutcome.NOT_APPLIED_AT_PARENT,
        )
    return CheckpointRefAdvancementInspection(
        outcome=CheckpointRefInspectionOutcome.NOT_APPLIED_ELSEWHERE,
    )


def _load_checkpoint_evidence(
    artifacts: ProtectedArtifactStore,
    run_id: str,
) -> SequenceCheckpointEvidence | None:
    path = artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT
    if not path.is_file():
        return None
    return SequenceCheckpointEvidence.model_validate_json(path.read_bytes())


def checkpoint_commit_evidence_exists(artifacts: ProtectedArtifactStore, run_id: str) -> bool:
    evidence = _load_checkpoint_evidence(artifacts, run_id)
    return evidence is not None and evidence.commit_sha256 is not None


def checkpoint_ref_may_have_advanced_for_commit(
    intent: SequenceCheckpointIntent,
    commit_sha256: str | None,
) -> bool:
    """Return True only when live HEAD read succeeds and equals commit_sha256."""
    inspection = inspect_checkpoint_ref_advancement(intent, commit_sha256)
    return inspection.outcome == CheckpointRefInspectionOutcome.APPLIED


def checkpoint_ref_may_have_advanced(
    artifacts: ProtectedArtifactStore,
    *,
    run_id: str,
    intent: SequenceCheckpointIntent,
    evidence: SequenceCheckpointEvidence | None,
) -> bool:
    """Return True only when live HEAD proves the branch ref points at the checkpoint commit."""
    if evidence is None:
        return False
    return checkpoint_ref_may_have_advanced_for_commit(intent, evidence.commit_sha256)


def checkpoint_cas_authorization_pending(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
) -> bool:
    """Return True when CAS was authorized but ref advancement is not yet recorded."""
    hold = store.get_checkpoint_reconciliation_hold_row(conn, run_id)
    if hold is None:
        return False
    if bool(int(hold["ref_may_have_advanced"])):
        return False
    return str(hold["hold_reason"]) == CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON


def checkpoint_cas_uncertainty_active(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
) -> bool:
    """Return True when a durable hold records CAS authorization or ref uncertainty."""
    hold = store.get_checkpoint_reconciliation_hold_row(conn, run_id)
    if hold is None:
        return False
    if bool(int(hold["ref_may_have_advanced"])):
        return True
    return str(hold["hold_reason"]) == CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON


def authorized_cas_abort_resolution_may_proceed(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    inspection: CheckpointRefAdvancementInspection,
    tick_owner_id: str,
    tick_lease_generation: int,
    now: datetime,
) -> bool:
    """Return True when a recovery tick may resolve an authorized-but-unapplied CAS hold."""
    from ai_dev_loop.scheduler.application.tick_fencing import tick_lease_is_active

    if inspection.outcome != CheckpointRefInspectionOutcome.NOT_APPLIED_AT_PARENT:
        return False
    if not store.has_abort_requested_for_run(conn, run_id):
        return False
    if not checkpoint_cas_authorization_pending(store, conn, run_id=run_id):
        return False
    if not abort_blocks_cas_authorization(
        store,
        conn,
        run_id=run_id,
        allow_post_cas_reconciliation=False,
    ):
        return False
    if store.get_nonterminal_attempt_for_run(conn, run_id) is not None:
        return False
    if store.has_pending_effects_for_run(conn, run_id):
        return False
    return tick_lease_is_active(
        store,
        conn,
        owner_id=tick_owner_id,
        generation=tick_lease_generation,
        now=now,
    )


def abort_blocks_cas_authorization(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    allow_post_cas_reconciliation: bool,
) -> bool:
    """Return True when acknowledged abort must block a new CAS authorization."""
    if allow_post_cas_reconciliation:
        return False
    return store.has_abort_requested_for_run(conn, run_id)


def should_defer_checkpoint_abort_transition(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    artifacts: ProtectedArtifactStore | None,
) -> bool:
    """Defer terminal abort only while post-CAS ref uncertainty remains unreconciled."""
    del artifacts
    return checkpoint_cas_uncertainty_active(store, conn, run_id=run_id)


def abort_blocks_checkpoint_mutation(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    state: SchedulerState,
    allow_post_cas_reconciliation: bool,
) -> bool:
    """Return True when acknowledged abort must block pre-reconciliation Git mutation."""
    if allow_post_cas_reconciliation:
        return False
    if checkpoint_cas_uncertainty_active(store, conn, run_id=run_id):
        return False
    if store.has_abort_requested_for_run(conn, run_id):
        return True
    return isinstance(state, AbortedState)
