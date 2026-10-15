"""Explicit acyclic ancestry for cursor and review recovery successors."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    CODEX_IMPLEMENTER_RECOVERY_SCHEMA_VERSION,
    CURSOR_INITIAL_RECOVERY_SCHEMA_VERSION,
    REVIEW_RETRY_SCHEMA_VERSION,
    SqliteSchedulerStore,
)


class RecoveryAncestryError(ValueError):
    """An ancestor edge is missing, cyclic, or owned by an unrelated run."""


@dataclass(frozen=True)
class RecoveryAncestorEdge:
    relation: Literal["cursor_recovery", "review_recovery", "implementer_recovery"]
    successor_run_id: str
    source_run_id: str
    recovery_key: str


def recovery_ancestor_edges(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> tuple[RecoveryAncestorEdge, ...]:
    """Walk parent edges from ``run_id`` toward the oldest authenticated source."""

    edges: list[RecoveryAncestorEdge] = []
    current = run_id
    visited = {run_id}
    schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    while True:
        if len(edges) >= 128:
            raise RecoveryAncestryError("recovery ancestry exceeds the supported bound")
        cursor_row = (
            store.get_cursor_initial_recovery_by_successor(
                conn,
                successor_run_id=current,
            )
            if schema_version >= CURSOR_INITIAL_RECOVERY_SCHEMA_VERSION
            else None
        )
        review_row = (
            store.get_review_recovery_source_for_successor(
                conn,
                successor_run_id=current,
            )
            if schema_version >= REVIEW_RETRY_SCHEMA_VERSION
            else None
        )
        implementer_row = (
            store.get_codex_implementer_recovery_by_successor(
                conn,
                successor_run_id=current,
            )
            if schema_version >= CODEX_IMPLEMENTER_RECOVERY_SCHEMA_VERSION
            else None
        )
        if cursor_row is not None and str(cursor_row["status"]) != "ready":
            cursor_row = None
        if implementer_row is not None and str(implementer_row["status"]) != "ready":
            implementer_row = None
        parents = sum(row is not None for row in (cursor_row, review_row, implementer_row))
        if parents > 1:
            raise RecoveryAncestryError("run has conflicting recovery parents")
        if parents == 0:
            return tuple(edges)
        if cursor_row is not None:
            relation: Literal["cursor_recovery", "review_recovery", "implementer_recovery"] = (
                "cursor_recovery"
            )
            source_run_id = str(cursor_row["source_run_id"])
            recovery_key = str(cursor_row["recovery_key"])
            successor_run_id = str(cursor_row["successor_run_id"])
        elif review_row is not None:
            assert review_row is not None
            relation = "review_recovery"
            source_run_id = str(review_row["source_run_id"])
            recovery_key = str(review_row["recovery_key"])
            successor_run_id = str(review_row["successor_run_id"])
        else:
            assert implementer_row is not None
            relation = "implementer_recovery"
            source_run_id = str(implementer_row["source_run_id"])
            recovery_key = str(implementer_row["recovery_key"])
            successor_run_id = str(implementer_row["successor_run_id"])
        if successor_run_id != current or not source_run_id or source_run_id == current:
            raise RecoveryAncestryError("recovery ancestor edge does not belong to this run")
        if source_run_id in visited:
            raise RecoveryAncestryError("recovery ancestry cycle")
        try:
            store.load_validated_snapshot(conn, source_run_id)
        except Exception as exc:
            raise RecoveryAncestryError("recovery ancestor source run is not an owner") from exc
        edges.append(
            RecoveryAncestorEdge(
                relation=relation,
                successor_run_id=successor_run_id,
                source_run_id=source_run_id,
                recovery_key=recovery_key,
            )
        )
        visited.add(source_run_id)
        current = source_run_id
