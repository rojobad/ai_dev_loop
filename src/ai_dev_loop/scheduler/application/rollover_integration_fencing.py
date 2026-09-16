"""Mutation fencing helpers for Phase 20.6.5 rollover integration."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ai_dev_loop.runners.git import CheckpointGitDeadline
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.tick_fencing import tick_lease_is_active
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@dataclass(frozen=True)
class RolloverIntegrationTickContext:
    tick_owner_id: str | None = None
    tick_lease_generation: int | None = None


def rollover_git_deadline(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    context: RolloverIntegrationTickContext | None,
) -> CheckpointGitDeadline:
    if context is None or context.tick_owner_id is None:
        return CheckpointGitDeadline.from_lease(None)
    row = store.get_tick_lease_row(conn)
    expires_at = row["expires_at"]
    if expires_at is None:
        return CheckpointGitDeadline.from_lease(None)
    from ai_dev_loop.scheduler.application.tick_fencing import parse_utc_instant

    return CheckpointGitDeadline.from_lease(parse_utc_instant(str(expires_at)))


def assert_rollover_mutation_allowed(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    rollover_id: str,
    worktree_key: str,
    rollover_version: int,
    integration_intent_sha256: str,
    context: RolloverIntegrationTickContext | None,
    now: datetime,
) -> None:
    row = store.get_authenticated_rollover(conn, rollover_id)
    if row is not None and int(row["version"]) != rollover_version:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "rollover version drift before integration mutation",
        )
    store.verify_rollover_target_reservation(
        conn,
        source_run_id=source_run_id,
        worktree_key=worktree_key,
    )
    if store.foreign_abort_hold_blocks_run(
        conn,
        source_run_id,
        allowed_checkpoint_intent_sha256=integration_intent_sha256,
    ):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "rollover integration blocked by foreign abort hold",
        )
    if context is not None and context.tick_owner_id is not None:
        if context.tick_lease_generation is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "rollover integration requires tick lease generation",
            )
        if not tick_lease_is_active(
            store,
            conn,
            owner_id=context.tick_owner_id,
            generation=context.tick_lease_generation,
            now=now,
        ):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "rollover integration tick lease expired",
            )
