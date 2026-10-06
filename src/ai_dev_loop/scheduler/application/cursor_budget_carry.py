"""Authenticated review-budget carry for Cursor correction recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from pydantic import ValidationError

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.recovery_ancestry import (
    RecoveryAncestryError,
    recovery_ancestor_edges,
)
from ai_dev_loop.scheduler.application.review_budget import (
    _validate_extension_event_row,
    fold_review_budget_extensions,
)
from ai_dev_loop.scheduler.domain.cursor_recovery_v2 import (
    CORRECTION_BUDGET_CARRY_REL,
    CORRECTION_RECORD_REL,
    BudgetCarryV1,
    CursorRecoveryRecordV2,
    ExtensionEvidenceV1,
)
from ai_dev_loop.scheduler.domain.events import (
    REVIEW_BUDGET_EXTENDED_EVENT_KIND,
    ReviewBudgetExtendedEvent,
)
from ai_dev_loop.scheduler.domain.state import SchedulerState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _corrupt(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.CORRUPTION, message)


def _events_for_run(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> tuple[tuple[ReviewBudgetExtendedEvent, ...], tuple[ExtensionEvidenceV1, ...]]:
    del store
    rows = conn.execute(
        """
        SELECT event_id, run_id, sequence, event_kind,
               event_payload, event_payload_sha256, created_at
        FROM scheduler_events
        WHERE run_id = ? AND event_kind = ?
        ORDER BY sequence ASC
        """,
        (run_id, REVIEW_BUDGET_EXTENDED_EVENT_KIND),
    ).fetchall()
    events: list[ReviewBudgetExtendedEvent] = []
    evidence: list[ExtensionEvidenceV1] = []
    prior_sequence: int | None = None
    for row in rows:
        event = _validate_extension_event_row(
            row,
            expected_run_id=run_id,
            prior_sequence=prior_sequence,
        )
        prior_sequence = int(row["sequence"])
        events.append(event)
        evidence.append(
            ExtensionEvidenceV1(
                owner_run_id=run_id,
                event_id=str(row["event_id"]),
                sequence=int(row["sequence"]),
                payload_sha256=str(row["event_payload_sha256"]),
                previous_effective_total=event.previous_effective_total,
                new_effective_total=event.new_effective_total,
            )
        )
    return tuple(events), tuple(evidence)


def _ready_cursor_row(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row | None:
    row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=run_id)
    if row is None or str(row["status"]) != "ready":
        return None
    return row


def _intent_is_v2(row: sqlite3.Row) -> bool:
    try:
        payload = json.loads(str(row["intent_payload"]))
    except json.JSONDecodeError as exc:
        raise _corrupt("cursor recovery publication intent is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise _corrupt("cursor recovery publication intent is not an object")
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _corrupt("cursor recovery publication intent version is invalid")
    return version == 2


def _nearest_v2_ancestor(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    source_run_id: str,
) -> str | None:
    """Closest ancestor whose ready cursor recovery row carries a v2 budget."""

    try:
        edges = recovery_ancestor_edges(store, conn, source_run_id)
    except RecoveryAncestryError as exc:
        raise _corrupt(str(exc)) from exc
    for edge in edges:
        row = _ready_cursor_row(store, conn, edge.source_run_id)
        if row is not None and _intent_is_v2(row):
            return edge.source_run_id
    return None


def _chain_runs(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> tuple[str, ...]:
    try:
        edges = recovery_ancestor_edges(store, conn, run_id)
    except RecoveryAncestryError as exc:
        raise _corrupt(str(exc)) from exc
    return tuple([edge.source_run_id for edge in reversed(edges)] + [run_id])


def build_budget_carry(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    successor_run_id: str,
    recovery_key: str,
    visited: set[str] | None = None,
) -> BudgetCarryV1:
    """Derive the carry from original extension events or the parent carry."""

    if visited is None:
        visited = set()
    if source_run_id in visited:
        raise _corrupt("budget carry ancestry cycle")
    state, _, _ = store.load_validated_snapshot(conn, source_run_id)
    submitted = state.context.workflow.max_review_iterations
    parent_row = _ready_cursor_row(store, conn, source_run_id)
    if parent_row is not None and _intent_is_v2(parent_row):
        parent_carry = load_budget_carry_for_run(
            store,
            artifacts,
            conn,
            source_run_id,
            visited=visited,
        )
        if parent_carry is None:
            raise _corrupt("parent correction recovery is missing its budget carry")
        local_events, local_evidence = _events_for_run(store, conn, source_run_id)
        ceiling = fold_review_budget_extensions(
            parent_carry.inherited_effective_ceiling,
            local_events,
        )
        completed = parent_carry.inherited_reviews_completed + store.count_review_completion_events(
            conn,
            source_run_id,
        )
        return BudgetCarryV1(
            schema_version=1,
            successor_run_id=successor_run_id,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
            submitted_max_review_iterations=submitted,
            inherited_effective_ceiling=ceiling,
            inherited_reviews_completed=completed,
            authority="parent_carry",
            extension_events=list(local_evidence),
            parent_carry_owner_run_id=source_run_id,
            parent_carry_path=CORRECTION_BUDGET_CARRY_REL,
            parent_carry_sha256=parent_carry.canonical_sha256(),
        )

    ancestor_owner = _nearest_v2_ancestor(store, conn, source_run_id)
    if ancestor_owner is not None:
        parent_carry = load_budget_carry_for_run(
            store,
            artifacts,
            conn,
            ancestor_owner,
            visited=visited,
        )
        if parent_carry is None:
            raise _corrupt("ancestor correction recovery is missing its budget carry")
        chain = _chain_runs(store, conn, source_run_id)
        applicable = chain[chain.index(ancestor_owner) :]
        event_groups: list[ReviewBudgetExtendedEvent] = []
        evidence: list[ExtensionEvidenceV1] = []
        completed = parent_carry.inherited_reviews_completed
        for run_id in applicable:
            events, found = _events_for_run(store, conn, run_id)
            event_groups.extend(events)
            evidence.extend(found)
            completed += store.count_review_completion_events(conn, run_id)
        ceiling = fold_review_budget_extensions(
            parent_carry.inherited_effective_ceiling,
            event_groups,
        )
        return BudgetCarryV1(
            schema_version=1,
            successor_run_id=successor_run_id,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
            submitted_max_review_iterations=submitted,
            inherited_effective_ceiling=ceiling,
            inherited_reviews_completed=completed,
            authority="parent_carry",
            extension_events=evidence,
            parent_carry_owner_run_id=ancestor_owner,
            parent_carry_path=CORRECTION_BUDGET_CARRY_REL,
            parent_carry_sha256=parent_carry.canonical_sha256(),
        )

    event_groups = []
    extension_evidence: list[ExtensionEvidenceV1] = []
    for run_id in _chain_runs(store, conn, source_run_id):
        if run_id != source_run_id:
            ancestor_row = _ready_cursor_row(store, conn, run_id)
            if ancestor_row is not None and _intent_is_v2(ancestor_row):
                raise _corrupt("original extension history is already represented by a budget carry")
        events, found = _events_for_run(store, conn, run_id)
        event_groups.extend(events)
        extension_evidence.extend(found)
    ceiling = fold_review_budget_extensions(submitted, event_groups)
    completed = 0
    for run_id in _chain_runs(store, conn, source_run_id):
        completed += store.count_review_completion_events(conn, run_id)
    return BudgetCarryV1(
        schema_version=1,
        successor_run_id=successor_run_id,
        source_run_id=source_run_id,
        recovery_key=recovery_key,
        submitted_max_review_iterations=submitted,
        inherited_effective_ceiling=ceiling,
        inherited_reviews_completed=completed,
        authority="extension_events",
        extension_events=extension_evidence,
        parent_carry_owner_run_id=None,
        parent_carry_path=None,
        parent_carry_sha256=None,
    )


def _assert_same_authority(stored: BudgetCarryV1, expected: BudgetCarryV1) -> None:
    if (
        stored.authority != expected.authority
        or stored.submitted_max_review_iterations != expected.submitted_max_review_iterations
        or stored.inherited_effective_ceiling != expected.inherited_effective_ceiling
        or stored.inherited_reviews_completed != expected.inherited_reviews_completed
        or stored.extension_events != expected.extension_events
        or stored.parent_carry_owner_run_id != expected.parent_carry_owner_run_id
        or stored.parent_carry_path != expected.parent_carry_path
        or stored.parent_carry_sha256 != expected.parent_carry_sha256
        or stored.source_run_id != expected.source_run_id
        or stored.recovery_key != expected.recovery_key
    ):
        raise _corrupt("budget carry does not match authenticated extension authority")


def load_budget_carry_for_run(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    run_id: str,
    *,
    visited: set[str] | None = None,
) -> BudgetCarryV1 | None:
    """Return the authenticated carry for a ready v2 successor, if this run is one."""

    if visited is None:
        visited = set()
    if run_id in visited:
        raise _corrupt("budget carry ancestry cycle")
    row = _ready_cursor_row(store, conn, run_id)
    if row is None:
        return None
    if not _intent_is_v2(row):
        from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
            authenticate_required_initial_ancestors,
        )

        # Schema-1 intents still carry required v2 initial records. Authenticate
        # them here, then keep the extension-event derivation used by v1.
        authenticate_required_initial_ancestors(store, artifacts, run_id)
        return None
    visited.add(run_id)
    record_sha = str(row["record_sha256"] or "")
    if not record_sha:
        raise _corrupt("correction recovery record digest is missing")
    try:
        record_bytes = artifacts.read_verified_bytes(
            run_id,
            CORRECTION_RECORD_REL,
            expected_sha256=record_sha,
        )
        record = CursorRecoveryRecordV2.model_validate_json(record_bytes)
    except (ProtectedArtifactError, OSError, ValidationError, UnicodeError) as exc:
        raise _corrupt("correction recovery record failed authentication") from exc
    if record.canonical_bytes() != record_bytes:
        raise _corrupt("correction recovery record is not canonical")
    if hashlib.sha256(record_bytes).hexdigest() != record_sha:
        raise _corrupt("correction recovery record digest mismatch")
    try:
        carry_bytes = artifacts.read_verified_bytes(
            run_id,
            record.budget_carry_path,
            expected_sha256=record.budget_carry_sha256,
        )
        carry = BudgetCarryV1.model_validate_json(carry_bytes)
    except (ProtectedArtifactError, OSError, ValidationError, UnicodeError) as exc:
        raise _corrupt("budget carry failed authentication") from exc
    if carry.canonical_bytes() != carry_bytes:
        raise _corrupt("budget carry is not canonical")
    if (
        carry.successor_run_id != run_id
        or carry.successor_run_id != record.successor_run_id
        or carry.source_run_id != record.source_run_id
        or carry.recovery_key != str(row["recovery_key"])
        or carry.inherited_effective_ceiling != record.effective_review_ceiling
        or carry.inherited_reviews_completed != record.reviews_completed
        or carry.submitted_max_review_iterations != record.submitted_max_review_iterations
        or record.budget_carry_path != CORRECTION_BUDGET_CARRY_REL
    ):
        raise _corrupt("budget carry disagrees with the correction recovery record")
    expected = build_budget_carry(
        store,
        artifacts,
        conn,
        source_run_id=carry.source_run_id,
        successor_run_id=carry.successor_run_id,
        recovery_key=carry.recovery_key,
        visited=set(visited),
    )
    _assert_same_authority(carry, expected)
    return carry


def inherited_ceiling_for_run(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    state: SchedulerState,
    artifacts: ProtectedArtifactStore | None,
    _visited: set[str] | None = None,
) -> int | None:
    """Inherited effective ceiling before this run's own extension events."""

    if _visited is None:
        _visited = set()
    if state.run_id in _visited:
        raise _corrupt("review budget ancestry cycle")
    _visited.add(state.run_id)
    if artifacts is None:
        root = store.db_path.parent / "artifacts"
        if not root.is_dir():
            row = _ready_cursor_row(store, conn, state.run_id)
            if row is not None and _intent_is_v2(row):
                raise _corrupt("budget carry artifacts are missing")
            return None
        artifacts = ProtectedArtifactStore(root)
    carry = load_budget_carry_for_run(store, artifacts, conn, state.run_id)
    if carry is not None:
        return carry.inherited_effective_ceiling
    parent = store.get_review_recovery_source_for_successor(conn, successor_run_id=state.run_id)
    if parent is None:
        return None
    parent_id = str(parent["source_run_id"])
    parent_state, _, _ = store.load_validated_snapshot(conn, parent_id)
    parent_base = inherited_ceiling_for_run(
        store,
        conn,
        parent_state,
        artifacts,
        _visited=_visited,
    )
    from ai_dev_loop.scheduler.application.review_budget import (
        effective_review_ceiling_for_state,
        load_review_budget_extensions,
    )

    parent_events = load_review_budget_extensions(store, conn, parent_id)
    return effective_review_ceiling_for_state(
        parent_state,
        parent_events,
        base_ceiling=parent_base,
    )


def authenticated_reviews_completed(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    run_id: str,
    *,
    visited: set[str] | None = None,
) -> int:
    """Completed reviews for a run, including cursor-carry and review-recovery ancestry."""

    if visited is None:
        visited = set()
    if run_id in visited:
        raise _corrupt("review budget ancestry cycle")
    visited.add(run_id)
    local = store.count_review_completion_events(conn, run_id)
    carry = load_budget_carry_for_run(store, artifacts, conn, run_id)
    if carry is not None:
        return carry.inherited_reviews_completed + local
    parent = store.get_review_recovery_source_for_successor(conn, successor_run_id=run_id)
    if parent is None:
        return local
    return (
        authenticated_reviews_completed(
            store,
            artifacts,
            conn,
            str(parent["source_run_id"]),
            visited=visited,
        )
        + local
    )


def inherited_completed_for_blocked_run(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
    artifacts: ProtectedArtifactStore | None,
) -> int | None:
    if artifacts is None:
        root = store.db_path.parent / "artifacts"
        if not root.is_dir():
            return None
        artifacts = ProtectedArtifactStore(root)
    carry = load_budget_carry_for_run(store, artifacts, conn, run_id)
    if carry is not None:
        return carry.inherited_reviews_completed
    parent = store.get_review_recovery_source_for_successor(conn, successor_run_id=run_id)
    if parent is None:
        return None
    return authenticated_reviews_completed(
        store,
        artifacts,
        conn,
        str(parent["source_run_id"]),
    )
