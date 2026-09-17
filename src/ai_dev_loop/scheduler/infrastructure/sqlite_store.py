"""SQLite persistence for the central scheduler engine."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ai_dev_loop.scheduler.application.contracts import (
    ReservationStatus,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant, payload_sha256
from ai_dev_loop.scheduler.domain.effects import (
    FAKE_AGENT_SELF_TEST_EFFECT_ID,
    FAKE_AGENT_SELF_TEST_EFFECT_KIND,
    SYNTHETIC_SELF_TEST_EFFECT_ID,
    SYNTHETIC_SELF_TEST_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.events import (
    SCHEDULER_EVENT_ADAPTER,
    RunAuthorizedEvent,
    RunSubmittedEvent,
    SchedulerEvent,
    parse_scheduler_event,
)
from ai_dev_loop.scheduler.domain.state import (
    SCHEDULER_STATE_ADAPTER,
    AuthorizedState,
    SchedulerState,
    SubmittedState,
)

if TYPE_CHECKING:
    from ai_dev_loop.scheduler.domain.events import SchedulerEvent
    from ai_dev_loop.scheduler.domain.sequence import (
        AbortedSequenceState,
        AbortPendingSequenceState,
        ActiveSequenceState,
        AwaitingFinalizationSequenceState,
        BlockedSequenceState,
        PreparedSequenceState,
    )

    SequencePersistedState = (
        PreparedSequenceState
        | ActiveSequenceState
        | AbortPendingSequenceState
        | BlockedSequenceState
        | AbortedSequenceState
        | AwaitingFinalizationSequenceState
    )

SCHEMA_VERSION = 11
SEQUENCE_SCHEMA_VERSION = 5
REVIEW_RETRY_SCHEMA_VERSION = 6
SEQUENCE_RUN_LINEAGE_SCHEMA_VERSION = 9
MIN_READONLY_SCHEMA_VERSION = 4
MIGRATION_V1_NAME = "0001_initial"
MIGRATION_V2_NAME = "0002_tick_control"
MIGRATION_V3_NAME = "0003_attempt_executor"
MIGRATION_V4_NAME = "0004_cursor_workflow"
MIGRATION_V5_NAME = "0005_sequence_definitions"
MIGRATION_V6_NAME = "0006_review_retry"
MIGRATION_V7_NAME = "0007_checkpoint_holds"
MIGRATION_V8_NAME = "0008_capacity_retry"
MIGRATION_V9_NAME = "0009_sequence_run_lineage"
MIGRATION_V10_NAME = "0010_sequence_review_recovery"
MIGRATION_V11_NAME = "0011_sequence_replacement_cancellation"
SEQUENCE_REVIEW_RECOVERY_SCHEMA_VERSION = 11
REQUIRED_TABLES = frozenset(
    {
        "scheduler_schema_migrations",
        "scheduler_runs",
        "scheduler_events",
        "scheduler_effects",
        "scheduler_timers",
        "scheduler_tick_leases",
        "scheduler_claims",
        "scheduler_attempts",
        "scheduler_repository_reservations",
        "scheduler_capacity",
        "scheduler_run_tick_claims",
        "scheduler_sequences",
        "scheduler_sequence_entries",
        "scheduler_review_retry_generations",
        "scheduler_review_recovery_successors",
        "scheduler_checkpoint_holds",
        "scheduler_capacity_retry_generations",
        "scheduler_sequence_run_attempts",
        "scheduler_sequence_execution_replacements",
    }
)
REQUIRED_TABLES_V9 = REQUIRED_TABLES - {"scheduler_sequence_execution_replacements"}
REQUIRED_TABLES_V8 = REQUIRED_TABLES_V9 - {"scheduler_sequence_run_attempts"}
REQUIRED_TABLES_V7 = REQUIRED_TABLES_V8 - {"scheduler_capacity_retry_generations"}
REQUIRED_INDEXES = frozenset(
    {
        "idx_scheduler_runs_state_kind",
        "idx_scheduler_runs_worktree",
        "idx_scheduler_events_run_seq",
        "idx_scheduler_effects_run_history",
        "idx_scheduler_effects_eligible",
        "idx_scheduler_effects_one_live_per_run",
        "idx_scheduler_timers_eligible",
        "idx_scheduler_timers_one_pending_per_run",
        "idx_scheduler_claims_run",
        "idx_scheduler_attempts_run",
        "idx_scheduler_attempts_dispatch",
        "idx_scheduler_attempts_unit_identity",
        "idx_scheduler_attempts_active_per_run",
        "idx_scheduler_reservations_run",
        "idx_scheduler_run_tick_claims_active_admission",
        "idx_scheduler_run_tick_claims_run",
        "idx_scheduler_runs_controller_repo",
        "idx_scheduler_sequences_worktree",
        "idx_scheduler_sequence_entries_sequence",
        "idx_scheduler_review_recovery_source",
        "idx_scheduler_checkpoint_holds_intent",
        "idx_scheduler_sequence_run_attempts_sequence",
        "idx_scheduler_sequence_execution_replacements_sequence",
    }
)
REQUIRED_TABLES_V6 = REQUIRED_TABLES_V7 - {"scheduler_checkpoint_holds"}
REQUIRED_INDEXES_V9 = REQUIRED_INDEXES - {"idx_scheduler_sequence_execution_replacements_sequence"}
REQUIRED_INDEXES_V8 = REQUIRED_INDEXES_V9 - {"idx_scheduler_sequence_run_attempts_sequence"}
REQUIRED_INDEXES_V6 = REQUIRED_INDEXES_V8 - {"idx_scheduler_checkpoint_holds_intent"}
REQUIRED_TABLES_V5 = REQUIRED_TABLES_V6 - {
    "scheduler_review_retry_generations",
    "scheduler_review_recovery_successors",
}
REQUIRED_TABLES_V4 = REQUIRED_TABLES_V5 - {
    "scheduler_sequences",
    "scheduler_sequence_entries",
}
REQUIRED_INDEXES_V5 = REQUIRED_INDEXES_V6 - {
    "idx_scheduler_review_recovery_source",
}
REQUIRED_INDEXES_V4 = REQUIRED_INDEXES_V5 - {
    "idx_scheduler_sequences_worktree",
    "idx_scheduler_sequence_entries_sequence",
}
CHECKPOINT_HOLD_SCHEMA_VERSION = 7
NON_TERMINAL_STATE_KINDS = frozenset(
    {
        "queued",
        "authorized",
        "admitted",
        "preflight_complete",
        "cursor_ready",
        "waiting_usage_limit",
        "waiting_codex_capacity",
        "awaiting_codex_review",
        "waiting_codex_review_retry",
        "waiting_for_cursor_fix",
        "checkpoint_pending",
        "blocked",
        "pending_sequence_review_recovery",
    }
)
TICK_ELIGIBLE_STATE_KINDS = frozenset(
    {
        "queued",
        "authorized",
        "admitted",
        "preflight_complete",
        "cursor_ready",
        "waiting_usage_limit",
        "waiting_codex_capacity",
        "awaiting_codex_review",
        "waiting_codex_review_retry",
        "waiting_for_cursor_fix",
        "checkpoint_pending",
    }
)
EFFECT_STATUS_PENDING = "pending"
EFFECT_STATUS_CLAIMED = "claimed"
EFFECT_STATUS_SUCCEEDED = "succeeded"
EFFECT_STATUS_SUPERSEDED = "superseded"
EFFECT_STATUS_BLOCKED = "blocked"
EFFECT_STATUS_CANCELLED = "cancelled"
TIMER_STATUS_PENDING = "pending"
TIMER_STATUS_FIRED = "fired"
TIMER_STATUS_CANCELLED = "cancelled"
TICK_LEASE_NAME = "global"
CAPACITY_NAME = "global_active_agent"
ATTEMPT_STATUS_LAUNCHING = "launching"
ATTEMPT_STATUS_ACTIVE = "active"
ATTEMPT_STATUS_COMPLETED = "completed"
ATTEMPT_STATUS_FAILED = "failed"
ATTEMPT_STATUS_CANCELLED = "cancelled"
ATTEMPT_STATUS_UNCERTAIN = "uncertain"
NON_TERMINAL_ATTEMPT_STATUSES = frozenset(
    {ATTEMPT_STATUS_LAUNCHING, ATTEMPT_STATUS_ACTIVE, ATTEMPT_STATUS_UNCERTAIN}
)


def _migration_sql(name: str) -> str:
    package = resources.files("ai_dev_loop.scheduler.infrastructure.migrations")
    return (package / name).read_text(encoding="utf-8")


def _migration_v1_sql() -> str:
    return _migration_sql("0001_initial.sql")


def _migration_v2_sql() -> str:
    return _migration_sql("0002_tick_control.sql")


def _migration_v3_sql() -> str:
    return _migration_sql("0003_attempt_executor.sql")


def _migration_v4_sql() -> str:
    return _migration_sql("0004_cursor_workflow.sql")


def _migration_v5_sql() -> str:
    return _migration_sql("0005_sequence_definitions.sql")


def _migration_v6_sql() -> str:
    return _migration_sql("0006_review_retry.sql")


def _migration_v7_sql() -> str:
    return _migration_sql("0007_checkpoint_holds.sql")


def _migration_v8_sql() -> str:
    return _migration_sql("0008_capacity_retry.sql")


def _migration_v9_sql() -> str:
    return _migration_sql("0009_sequence_run_lineage.sql")


def _migration_v10_sql() -> str:
    return _migration_sql("0010_sequence_review_recovery.sql")


def _migration_v11_sql() -> str:
    return _migration_sql("0011_sequence_replacement_cancellation.sql")


def _split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    for chunk in sql.split(";"):
        lines = []
        for line in chunk.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            lines.append(line)
        text = "\n".join(lines).strip()
        if text:
            statements.append(text)
    return statements


def migration_checksum(version: int) -> str:
    if version == 1:
        return hashlib.sha256(_migration_v1_sql().encode("utf-8")).hexdigest()
    if version == 2:
        return hashlib.sha256(_migration_v2_sql().encode("utf-8")).hexdigest()
    if version == 3:
        return hashlib.sha256(_migration_v3_sql().encode("utf-8")).hexdigest()
    if version == 4:
        return hashlib.sha256(_migration_v4_sql().encode("utf-8")).hexdigest()
    if version == 5:
        return hashlib.sha256(_migration_v5_sql().encode("utf-8")).hexdigest()
    if version == 6:
        return hashlib.sha256(_migration_v6_sql().encode("utf-8")).hexdigest()
    if version == 7:
        return hashlib.sha256(_migration_v7_sql().encode("utf-8")).hexdigest()
    if version == 8:
        return hashlib.sha256(_migration_v8_sql().encode("utf-8")).hexdigest()
    if version == 9:
        return hashlib.sha256(_migration_v9_sql().encode("utf-8")).hexdigest()
    if version == 10:
        return hashlib.sha256(_migration_v10_sql().encode("utf-8")).hexdigest()
    if version == 11:
        return hashlib.sha256(_migration_v11_sql().encode("utf-8")).hexdigest()
    raise ValueError(f"unsupported migration version {version}")


class SqliteSchedulerStore:
    """One-connection-per-operation SQLite store for the central scheduler."""

    def __init__(
        self,
        db_path: Path,
        *,
        busy_timeout_ms: int = 5000,
        migration_fault_hook: Callable[[str], None] | None = None,
        bootstrap: bool = True,
        read_only: bool = False,
    ) -> None:
        if busy_timeout_ms < 0:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "busy_timeout_ms must be >= 0",
            )
        from ai_dev_loop.scheduler.infrastructure.paths import (
            apply_database_permissions,
            secure_database_path,
        )

        self.db_path = secure_database_path(Path(db_path), create_parent=not read_only)
        self.busy_timeout_ms = busy_timeout_ms
        self._migration_fault_hook = migration_fault_hook
        self._apply_database_permissions = apply_database_permissions
        self._read_only = read_only
        if bootstrap and not read_only:
            self.bootstrap()

    @classmethod
    def open_readonly(
        cls,
        db_path: Path,
        *,
        busy_timeout_ms: int = 5000,
    ) -> SqliteSchedulerStore:
        """Open an existing scheduler database without creating or migrating it."""

        if busy_timeout_ms < 0:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "busy_timeout_ms must be >= 0",
            )
        from ai_dev_loop.scheduler.infrastructure.paths import (
            apply_database_permissions,
            secure_database_path,
        )

        resolved = secure_database_path(Path(db_path), create_parent=False)
        if not resolved.exists():
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.NOT_FOUND,
                "scheduler database not found",
            )
        store = cls.__new__(cls)
        store.db_path = resolved
        store.busy_timeout_ms = busy_timeout_ms
        store._migration_fault_hook = None
        store._apply_database_permissions = apply_database_permissions
        store._read_only = True
        with store._connect() as conn:
            version = store._user_version(conn)
            if version == 0:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.NOT_FOUND,
                    "scheduler database not initialized",
                )
            if version > SCHEMA_VERSION:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.SCHEMA,
                    f"database user_version {version} is newer than supported {SCHEMA_VERSION}",
                )
            if version < MIN_READONLY_SCHEMA_VERSION:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.SCHEMA,
                    f"unsupported schema version {version}",
                )
            store._verify_current_schema(conn, expected_version=version)
        return store

    def bootstrap(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._configure(conn)
            version = self._user_version(conn)
            if version > SCHEMA_VERSION:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.SCHEMA,
                    f"database user_version {version} is newer than supported {SCHEMA_VERSION}",
                )
            if version == 0:
                self._bootstrap_v1(conn)
                self._migrate_v1_to_v2(conn)
                self._migrate_v2_to_v3(conn)
                self._migrate_v3_to_v4(conn)
                self._migrate_v4_to_v5(conn)
                self._migrate_v5_to_v6(conn)
                self._migrate_v6_to_v7(conn)
                self._migrate_v7_to_v8(conn)
                self._migrate_v8_to_v9(conn)
                self._migrate_v9_to_v10(conn)
                self._migrate_v10_to_v11(conn)
            elif version == 1:
                self._verify_migration_checksum(conn, 1)
                self._migrate_v1_to_v2(conn)
                self._migrate_v2_to_v3(conn)
                self._migrate_v3_to_v4(conn)
                self._migrate_v4_to_v5(conn)
                self._migrate_v5_to_v6(conn)
                self._migrate_v6_to_v7(conn)
                self._migrate_v7_to_v8(conn)
                self._migrate_v8_to_v9(conn)
                self._migrate_v9_to_v10(conn)
                self._migrate_v10_to_v11(conn)
            elif version == 2:
                self._verify_migration_checksum(conn, 1)
                self._verify_migration_checksum(conn, 2)
                self._migrate_v2_to_v3(conn)
                self._migrate_v3_to_v4(conn)
                self._migrate_v4_to_v5(conn)
                self._migrate_v5_to_v6(conn)
                self._migrate_v6_to_v7(conn)
                self._migrate_v7_to_v8(conn)
                self._migrate_v8_to_v9(conn)
                self._migrate_v9_to_v10(conn)
                self._migrate_v10_to_v11(conn)
            elif version == 3:
                for migration_version in (1, 2, 3):
                    self._verify_migration_checksum(conn, migration_version)
                self._migrate_v3_to_v4(conn)
                self._migrate_v4_to_v5(conn)
                self._migrate_v5_to_v6(conn)
                self._migrate_v6_to_v7(conn)
                self._migrate_v7_to_v8(conn)
                self._migrate_v8_to_v9(conn)
                self._migrate_v9_to_v10(conn)
                self._migrate_v10_to_v11(conn)
            elif version == 4:
                for migration_version in (1, 2, 3, 4):
                    self._verify_migration_checksum(conn, migration_version)
                self._migrate_v4_to_v5(conn)
                self._migrate_v5_to_v6(conn)
                self._migrate_v6_to_v7(conn)
                self._migrate_v7_to_v8(conn)
                self._migrate_v8_to_v9(conn)
                self._migrate_v9_to_v10(conn)
                self._migrate_v10_to_v11(conn)
            elif version == 5:
                for migration_version in (1, 2, 3, 4, 5):
                    self._verify_migration_checksum(conn, migration_version)
                self._migrate_v5_to_v6(conn)
                self._migrate_v6_to_v7(conn)
                self._migrate_v7_to_v8(conn)
                self._migrate_v8_to_v9(conn)
                self._migrate_v9_to_v10(conn)
                self._migrate_v10_to_v11(conn)
            elif version == 6:
                for migration_version in (1, 2, 3, 4, 5, 6):
                    self._verify_migration_checksum(conn, migration_version)
                self._migrate_v6_to_v7(conn)
                self._migrate_v7_to_v8(conn)
                self._migrate_v8_to_v9(conn)
                self._migrate_v9_to_v10(conn)
                self._migrate_v10_to_v11(conn)
            elif version == 7:
                for migration_version in (1, 2, 3, 4, 5, 6, 7):
                    self._verify_migration_checksum(conn, migration_version)
                self._migrate_v7_to_v8(conn)
                self._migrate_v8_to_v9(conn)
                self._migrate_v9_to_v10(conn)
                self._migrate_v10_to_v11(conn)
            elif version == 8:
                for migration_version in (1, 2, 3, 4, 5, 6, 7, 8):
                    self._verify_migration_checksum(conn, migration_version)
                self._migrate_v8_to_v9(conn)
                self._migrate_v9_to_v10(conn)
                self._migrate_v10_to_v11(conn)
            elif version == 9:
                for migration_version in (1, 2, 3, 4, 5, 6, 7, 8, 9):
                    self._verify_migration_checksum(conn, migration_version)
                self._migrate_v9_to_v10(conn)
                self._migrate_v10_to_v11(conn)
            elif version == 10:
                for migration_version in range(1, 11):
                    self._verify_migration_checksum(conn, migration_version)
                self._migrate_v10_to_v11(conn)
            else:
                self._verify_current_schema(conn)
            self._apply_database_permissions(self.db_path)

    def _bootstrap_v1(self, conn: sqlite3.Connection) -> None:
        tables = self._table_names(conn)
        if tables - {"sqlite_sequence"}:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                "non-empty v0 database refuses automatic migration",
            )
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split_sql_statements(_migration_v1_sql()):
                self._fault_maybe_raise_migration(statement)
                conn.execute(statement)
            checksum = migration_checksum(1)
            applied_at = encode_utc_instant(datetime.now(tz=UTC))
            conn.execute(
                """
                INSERT INTO scheduler_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (1, MIGRATION_V1_NAME, checksum, applied_at),
            )
            conn.execute(
                """
                INSERT INTO scheduler_tick_leases(
                    lease_name, owner_id, generation, status,
                    acquired_at, expires_at, updated_at
                ) VALUES ('global', NULL, 0, 'inactive', NULL, NULL, ?)
                """,
                (applied_at,),
            )
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._apply_database_permissions(self.db_path)

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        if self._user_version(conn) >= 2:
            self._verify_current_schema(conn)
            return
        self._verify_migration_checksum(conn, 1)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split_sql_statements(_migration_v2_sql()):
                self._fault_maybe_raise_migration(statement)
                conn.execute(statement)
            applied_at = encode_utc_instant(datetime.now(tz=UTC))
            conn.execute(
                """
                INSERT INTO scheduler_capacity(
                    capacity_name, max_value, holder_run_id, holder_claim_id,
                    holder_tick_generation, updated_at
                ) VALUES (?, 1, NULL, NULL, NULL, ?)
                """,
                (CAPACITY_NAME, applied_at),
            )
            conn.execute(
                """
                INSERT INTO scheduler_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (2, MIGRATION_V2_NAME, migration_checksum(2), applied_at),
            )
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._apply_database_permissions(self.db_path)

    def _migrate_v2_to_v3(self, conn: sqlite3.Connection) -> None:
        if self._user_version(conn) >= 3:
            self._verify_current_schema(conn)
            return
        for migration_version in (1, 2):
            self._verify_migration_checksum(conn, migration_version)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split_sql_statements(_migration_v3_sql()):
                self._fault_maybe_raise_migration(statement)
                conn.execute(statement)
            applied_at = encode_utc_instant(datetime.now(tz=UTC))
            conn.execute(
                """
                INSERT INTO scheduler_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (3, MIGRATION_V3_NAME, migration_checksum(3), applied_at),
            )
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._apply_database_permissions(self.db_path)

    def _migrate_v3_to_v4(self, conn: sqlite3.Connection) -> None:
        if self._user_version(conn) >= 4:
            self._verify_current_schema(conn)
            return
        for migration_version in (1, 2, 3):
            self._verify_migration_checksum(conn, migration_version)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split_sql_statements(_migration_v4_sql()):
                self._fault_maybe_raise_migration(statement)
                conn.execute(statement)
            applied_at = encode_utc_instant(datetime.now(tz=UTC))
            conn.execute(
                """
                INSERT INTO scheduler_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (4, MIGRATION_V4_NAME, migration_checksum(4), applied_at),
            )
            conn.execute("PRAGMA user_version = 4")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._apply_database_permissions(self.db_path)

    def _migrate_v4_to_v5(self, conn: sqlite3.Connection) -> None:
        if self._user_version(conn) >= 5:
            self._verify_current_schema(conn)
            return
        for migration_version in (1, 2, 3, 4):
            self._verify_migration_checksum(conn, migration_version)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split_sql_statements(_migration_v5_sql()):
                self._fault_maybe_raise_migration(statement)
                conn.execute(statement)
            applied_at = encode_utc_instant(datetime.now(tz=UTC))
            conn.execute(
                """
                INSERT INTO scheduler_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (5, MIGRATION_V5_NAME, migration_checksum(5), applied_at),
            )
            conn.execute("PRAGMA user_version = 5")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._apply_database_permissions(self.db_path)

    def _migrate_v5_to_v6(self, conn: sqlite3.Connection) -> None:
        if self._user_version(conn) >= 6:
            self._verify_current_schema(conn)
            return
        for migration_version in (1, 2, 3, 4, 5):
            self._verify_migration_checksum(conn, migration_version)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split_sql_statements(_migration_v6_sql()):
                self._fault_maybe_raise_migration(statement)
                conn.execute(statement)
            applied_at = encode_utc_instant(datetime.now(tz=UTC))
            conn.execute(
                """
                INSERT INTO scheduler_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (6, MIGRATION_V6_NAME, migration_checksum(6), applied_at),
            )
            conn.execute("PRAGMA user_version = 6")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._apply_database_permissions(self.db_path)

    def _migrate_v6_to_v7(self, conn: sqlite3.Connection) -> None:
        if self._user_version(conn) >= 7:
            self._verify_current_schema(conn)
            return
        for migration_version in (1, 2, 3, 4, 5, 6):
            self._verify_migration_checksum(conn, migration_version)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split_sql_statements(_migration_v7_sql()):
                self._fault_maybe_raise_migration(statement)
                conn.execute(statement)
            applied_at = encode_utc_instant(datetime.now(tz=UTC))
            conn.execute(
                """
                INSERT INTO scheduler_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (7, MIGRATION_V7_NAME, migration_checksum(7), applied_at),
            )
            conn.execute("PRAGMA user_version = 7")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._apply_database_permissions(self.db_path)

    def _migrate_v7_to_v8(self, conn: sqlite3.Connection) -> None:
        if self._user_version(conn) >= 8:
            self._verify_current_schema(conn)
            return
        for migration_version in (1, 2, 3, 4, 5, 6, 7):
            self._verify_migration_checksum(conn, migration_version)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split_sql_statements(_migration_v8_sql()):
                self._fault_maybe_raise_migration(statement)
                conn.execute(statement)
            applied_at = encode_utc_instant(datetime.now(tz=UTC))
            conn.execute(
                """
                INSERT INTO scheduler_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (8, MIGRATION_V8_NAME, migration_checksum(8), applied_at),
            )
            conn.execute("PRAGMA user_version = 8")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._apply_database_permissions(self.db_path)

    def _migrate_v8_to_v9(self, conn: sqlite3.Connection) -> None:
        if self._user_version(conn) >= 9:
            self._verify_current_schema(conn)
            return
        for migration_version in (1, 2, 3, 4, 5, 6, 7, 8):
            self._verify_migration_checksum(conn, migration_version)
        from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
            backfill_historical_lineage_for_sequence,
        )

        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split_sql_statements(_migration_v9_sql()):
                self._fault_maybe_raise_migration(statement)
                conn.execute(statement)
            sequence_rows = conn.execute(
                "SELECT sequence_id FROM scheduler_sequences ORDER BY prepared_at ASC, sequence_id ASC"
            ).fetchall()
            for row in sequence_rows:
                backfill_historical_lineage_for_sequence(conn, self, str(row["sequence_id"]))
            applied_at = encode_utc_instant(datetime.now(tz=UTC))
            conn.execute(
                """
                INSERT INTO scheduler_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (9, MIGRATION_V9_NAME, migration_checksum(9), applied_at),
            )
            conn.execute("PRAGMA user_version = 9")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._apply_database_permissions(self.db_path)

    def _migrate_v9_to_v10(self, conn: sqlite3.Connection) -> None:
        if self._user_version(conn) >= 10:
            self._verify_current_schema(conn)
            return
        for migration_version in (1, 2, 3, 4, 5, 6, 7, 8, 9):
            self._verify_migration_checksum(conn, migration_version)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split_sql_statements(_migration_v10_sql()):
                self._fault_maybe_raise_migration(statement)
                conn.execute(statement)
            applied_at = encode_utc_instant(datetime.now(tz=UTC))
            conn.execute(
                """
                INSERT INTO scheduler_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (10, MIGRATION_V10_NAME, migration_checksum(10), applied_at),
            )
            conn.execute("PRAGMA user_version = 10")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._apply_database_permissions(self.db_path)

    def _migrate_v10_to_v11(self, conn: sqlite3.Connection) -> None:
        if self._user_version(conn) >= 11:
            self._verify_current_schema(conn)
            return
        for migration_version in range(1, 11):
            self._verify_migration_checksum(conn, migration_version)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split_sql_statements(_migration_v11_sql()):
                self._fault_maybe_raise_migration(statement)
                conn.execute(statement)
            applied_at = encode_utc_instant(datetime.now(tz=UTC))
            conn.execute(
                """
                INSERT INTO scheduler_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (11, MIGRATION_V11_NAME, migration_checksum(11), applied_at),
            )
            conn.execute("PRAGMA user_version = 11")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._apply_database_permissions(self.db_path)

    def _fault_maybe_raise_migration(self, statement: str) -> None:
        hook = self._migration_fault_hook
        if hook is not None:
            hook(statement)

    def _verify_migration_checksum(self, conn: sqlite3.Connection, version: int) -> None:
        row = conn.execute(
            "SELECT checksum FROM scheduler_schema_migrations WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                f"migration audit row missing for version {version}",
            )
        if row[0] != migration_checksum(version):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                "migration checksum drift detected",
            )

    def _verify_current_schema(
        self,
        conn: sqlite3.Connection,
        *,
        expected_version: int | None = None,
    ) -> None:
        version = self._user_version(conn)
        target = SCHEMA_VERSION if expected_version is None else expected_version
        if version != target:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                f"unsupported schema version {version}",
            )
        for migration_version in range(1, target + 1):
            self._verify_migration_checksum(conn, migration_version)
        if target >= SCHEMA_VERSION:
            tables = REQUIRED_TABLES
            indexes = REQUIRED_INDEXES
        elif target >= 9:
            tables = REQUIRED_TABLES_V9
            indexes = REQUIRED_INDEXES_V9
        elif target >= 8:
            tables = REQUIRED_TABLES_V8
            indexes = REQUIRED_INDEXES_V8
        elif target >= 7:
            tables = REQUIRED_TABLES_V7
            indexes = REQUIRED_INDEXES_V8
        elif target >= 6:
            tables = REQUIRED_TABLES_V6
            indexes = REQUIRED_INDEXES_V6
        elif target >= SEQUENCE_SCHEMA_VERSION:
            tables = REQUIRED_TABLES_V5
            indexes = REQUIRED_INDEXES_V5
        else:
            tables = REQUIRED_TABLES_V4
            indexes = REQUIRED_INDEXES_V4
        missing = tables - self._table_names(conn)
        if missing:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                f"corrupt schema missing tables: {sorted(missing)}",
            )
        missing_idx = indexes - self._index_names(conn)
        if missing_idx:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                f"corrupt schema missing indexes: {sorted(missing_idx)}",
            )

    def require_sequence_schema(self, conn: sqlite3.Connection) -> None:
        version = self._user_version(conn)
        if version < SEQUENCE_SCHEMA_VERSION:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                "prepared sequences require scheduler schema version 5",
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            if self._read_only:
                uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
                conn = sqlite3.connect(
                    uri,
                    uri=True,
                    isolation_level=None,
                    timeout=self.busy_timeout_ms / 1000.0,
                    check_same_thread=False,
                )
            else:
                conn = sqlite3.connect(
                    self.db_path,
                    isolation_level=None,
                    timeout=self.busy_timeout_ms / 1000.0,
                    check_same_thread=False,
                )
        except sqlite3.Error as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.INTERNAL,
                f"failed to open database: {exc.__class__.__name__}",
            ) from exc
        conn.row_factory = sqlite3.Row
        try:
            self._configure(conn)
            yield conn
        finally:
            conn.close()
            if not self._read_only and self.db_path.exists():
                self._apply_database_permissions(self.db_path)

    def _configure(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        if not self._read_only:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")

    @staticmethod
    def _user_version(conn: sqlite3.Connection) -> int:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def _table_names(conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {str(row[0]) for row in rows}

    @staticmethod
    def _index_names(conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name IS NOT NULL"
        ).fetchall()
        return {str(row[0]) for row in rows}

    @contextmanager
    def begin_immediate(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise SchedulerEngineError(
                        SchedulerEngineErrorKind.BUSY,
                        "database is busy",
                    ) from exc
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.INTERNAL,
                    f"begin failed: {exc.__class__.__name__}",
                ) from exc
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def begin_read(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def dump_state(state: SchedulerState) -> tuple[str, str, str]:
        validated = SCHEDULER_STATE_ADAPTER.validate_python(state.model_dump(mode="json"))
        text = SCHEDULER_STATE_ADAPTER.dump_json(validated).decode("utf-8")
        return validated.kind, text, payload_sha256(text)

    @staticmethod
    def load_state(payload: str) -> SchedulerState:
        return SCHEDULER_STATE_ADAPTER.validate_json(payload)

    @staticmethod
    def dump_event(event: SchedulerEvent) -> tuple[str, str, str]:
        validated = SCHEDULER_EVENT_ADAPTER.validate_python(event.model_dump(mode="json"))
        text = SCHEDULER_EVENT_ADAPTER.dump_json(validated).decode("utf-8")
        return validated.kind, text, payload_sha256(text)

    @staticmethod
    def load_event(payload: str) -> SchedulerEvent:
        return parse_scheduler_event(SCHEDULER_EVENT_ADAPTER.validate_json(payload))

    def get_run_by_idempotency_key(
        self, conn: sqlite3.Connection, idempotency_key: str
    ) -> sqlite3.Row | None:
        row = conn.execute(
            "SELECT * FROM scheduler_runs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def find_existing_submission(
        self,
        conn: sqlite3.Connection,
        *,
        context: object,
        resubmission_id: str | None,
    ) -> sqlite3.Row | None:
        from ai_dev_loop.scheduler.application.submission import (
            submission_idempotency_key_candidates,
        )
        from ai_dev_loop.scheduler.domain.state import (
            SubmittedRunContext,
            submission_identity_payload,
        )

        if not isinstance(context, SubmittedRunContext):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.INTERNAL,
                "submission context has unexpected type",
            )
        for key in submission_idempotency_key_candidates(
            context,
            resubmission_id=resubmission_id,
        ):
            existing = self.get_run_by_idempotency_key(conn, key)
            if existing is not None:
                return existing
        return self.find_run_by_matching_submission_identity(
            conn,
            worktree_key=context.repository.worktree_key,
            identity=submission_identity_payload(context),
            resubmission_id=resubmission_id,
        )

    def find_run_by_matching_submission_identity(
        self,
        conn: sqlite3.Connection,
        *,
        worktree_key: str,
        identity: dict[str, object],
        resubmission_id: str | None,
    ) -> sqlite3.Row | None:
        from ai_dev_loop.scheduler.application.submission import (
            submission_idempotency_key_candidates,
        )
        from ai_dev_loop.scheduler.domain.state import submission_identity_payload

        rows = conn.execute(
            """
            SELECT run_id FROM scheduler_runs
            WHERE worktree_key = ?
            ORDER BY created_at ASC, run_id ASC
            """,
            (worktree_key,),
        ).fetchall()
        for row in rows:
            run_id = str(row[0])
            state, _, _ = self.load_validated_snapshot(conn, run_id)
            if submission_identity_payload(state.context) != identity:
                continue
            stored_keys = submission_idempotency_key_candidates(
                state.context,
                resubmission_id=resubmission_id,
            )
            if state.idempotency_key in stored_keys:
                existing = self.get_run_by_idempotency_key(conn, state.idempotency_key)
                if existing is not None:
                    return existing
        return None

    def count_review_completion_events(self, conn: sqlite3.Connection, run_id: str) -> int:
        from ai_dev_loop.scheduler.application.contracts import REVIEW_COMPLETION_EVENT_KINDS

        placeholders = ",".join("?" * len(REVIEW_COMPLETION_EVENT_KINDS))
        row = conn.execute(
            f"""
            SELECT COUNT(*) FROM scheduler_events
            WHERE run_id = ? AND event_kind IN ({placeholders})
            """,
            (run_id, *REVIEW_COMPLETION_EVENT_KINDS),
        ).fetchone()
        if row is None:
            return 0
        return int(row[0])

    def get_worktree_reservation(
        self, conn: sqlite3.Connection, worktree_key: str
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT * FROM scheduler_repository_reservations
            WHERE worktree_key = ? AND status = ?
            """,
            (worktree_key, ReservationStatus.ACTIVE.value),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def get_active_reservation(
        self, conn: sqlite3.Connection, worktree_key: str
    ) -> sqlite3.Row | None:
        placeholders = ",".join("?" * len(NON_TERMINAL_STATE_KINDS))
        row = conn.execute(
            f"""
            SELECT r.*, s.state_kind
            FROM scheduler_repository_reservations r
            JOIN scheduler_runs s ON s.run_id = r.run_id
            WHERE r.worktree_key = ?
              AND r.status = ?
              AND s.state_kind IN ({placeholders})
            """,
            (worktree_key, ReservationStatus.ACTIVE.value, *NON_TERMINAL_STATE_KINDS),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def insert_submitted_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        state: SubmittedState,
        event_id: str,
        event: SchedulerEvent,
        now: datetime,
    ) -> None:
        if state.run_id != run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "SubmittedState.run_id must equal requested run_id",
            )
        kind, payload, digest = self.dump_state(state)
        if kind != "queued":
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "submission accepts only queued state",
            )
        now_text = encode_utc_instant(now)
        worktree_key = state.context.repository.worktree_key
        try:
            conn.execute(
                """
                INSERT INTO scheduler_runs(
                    run_id, state_kind, state_payload, state_payload_sha256,
                    version, idempotency_key, worktree_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    kind,
                    payload,
                    digest,
                    state.version,
                    state.idempotency_key,
                    worktree_key,
                    now_text,
                    now_text,
                ),
            )
            event_kind, event_payload, event_digest = self.dump_event(event)
            conn.execute(
                """
                INSERT INTO scheduler_events(
                    event_id, run_id, sequence, event_kind,
                    event_payload, event_payload_sha256, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (event_id, run_id, event_kind, event_payload, event_digest, now_text),
            )
            reservation = conn.execute(
                """
                INSERT INTO scheduler_repository_reservations(
                    worktree_key, run_id, repository_root, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(worktree_key) DO UPDATE SET
                    run_id = excluded.run_id,
                    repository_root = excluded.repository_root,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                WHERE scheduler_repository_reservations.status = 'released'
                """,
                (
                    worktree_key,
                    run_id,
                    state.context.repository.root,
                    ReservationStatus.ACTIVE.value,
                    now_text,
                    now_text,
                ),
            )
            if reservation.rowcount != 1:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "repository worktree reservation could not be claimed",
                )
        except sqlite3.IntegrityError as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "run insert conflict",
            ) from exc

    def insert_materialized_sequence_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        submitted_state: SubmittedState,
        authorized_state: AuthorizedState,
        submitted_event_id: str,
        submitted_event: RunSubmittedEvent,
        authorized_event_id: str,
        authorized_event: RunAuthorizedEvent,
        now: datetime,
    ) -> None:
        from ai_dev_loop.scheduler.domain.state import AuthorizedState as AuthorizedStateModel

        if submitted_state.run_id != run_id or authorized_state.run_id != run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "materialized run state run_id must equal requested run_id",
            )
        if not isinstance(authorized_state, AuthorizedStateModel):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "materialized run must end in authorized state",
            )
        if authorized_state.kind != "authorized":
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "materialized run must end in authorized state",
            )
        queued_kind, queued_payload, queued_digest = self.dump_state(submitted_state)
        if queued_kind != "queued":
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "materialized run requires queued submitted state",
            )
        authorized_kind, authorized_payload, authorized_digest = self.dump_state(authorized_state)
        if authorized_kind != "authorized":
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "materialized run requires authorized final state",
            )
        now_text = encode_utc_instant(now)
        worktree_key_value = authorized_state.context.repository.worktree_key
        try:
            conn.execute(
                """
                INSERT INTO scheduler_runs(
                    run_id, state_kind, state_payload, state_payload_sha256,
                    version, idempotency_key, worktree_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    authorized_kind,
                    authorized_payload,
                    authorized_digest,
                    authorized_state.version,
                    authorized_state.idempotency_key,
                    worktree_key_value,
                    now_text,
                    now_text,
                ),
            )
            submitted_kind, submitted_payload, submitted_digest = self.dump_event(submitted_event)
            conn.execute(
                """
                INSERT INTO scheduler_events(
                    event_id, run_id, sequence, event_kind,
                    event_payload, event_payload_sha256, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    submitted_event_id,
                    run_id,
                    submitted_kind,
                    submitted_payload,
                    submitted_digest,
                    now_text,
                ),
            )
            authorized_event_kind, authorized_event_payload, authorized_event_digest = (
                self.dump_event(authorized_event)
            )
            conn.execute(
                """
                INSERT INTO scheduler_events(
                    event_id, run_id, sequence, event_kind,
                    event_payload, event_payload_sha256, created_at
                ) VALUES (?, ?, 2, ?, ?, ?, ?)
                """,
                (
                    authorized_event_id,
                    run_id,
                    authorized_event_kind,
                    authorized_event_payload,
                    authorized_event_digest,
                    now_text,
                ),
            )
            reservation = conn.execute(
                """
                INSERT INTO scheduler_repository_reservations(
                    worktree_key, run_id, repository_root, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(worktree_key) DO UPDATE SET
                    run_id = excluded.run_id,
                    repository_root = excluded.repository_root,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                WHERE scheduler_repository_reservations.status = 'released'
                """,
                (
                    worktree_key_value,
                    run_id,
                    authorized_state.context.repository.root,
                    ReservationStatus.ACTIVE.value,
                    now_text,
                    now_text,
                ),
            )
            if reservation.rowcount != 1:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "repository worktree reservation could not be claimed",
                )
        except sqlite3.IntegrityError as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "materialized sequence run insert conflict",
            ) from exc

    def _insert_handoff_successor_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        submitted_state: object,
        authorized_state: object,
        submitted_event_id: str,
        submitted_event: SchedulerEvent,
        authorized_event_id: str,
        authorized_event: SchedulerEvent,
        now: datetime,
    ) -> None:
        from ai_dev_loop.scheduler.domain.state import AuthorizedState as AuthorizedStateModel
        from ai_dev_loop.scheduler.domain.state import SubmittedState as SubmittedStateModel

        if not isinstance(submitted_state, SubmittedStateModel):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "handoff successor requires SubmittedState",
            )
        if not isinstance(authorized_state, AuthorizedStateModel):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "handoff successor requires AuthorizedState",
            )
        queued_kind, queued_payload, queued_digest = self.dump_state(submitted_state)
        authorized_kind, authorized_payload, authorized_digest = self.dump_state(authorized_state)
        now_text = encode_utc_instant(now)
        worktree_key_value = authorized_state.context.repository.worktree_key
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                authorized_kind,
                authorized_payload,
                authorized_digest,
                authorized_state.version,
                authorized_state.idempotency_key,
                worktree_key_value,
                now_text,
                now_text,
            ),
        )
        submitted_kind, submitted_payload, submitted_digest = self.dump_event(submitted_event)
        conn.execute(
            """
            INSERT INTO scheduler_events(
                event_id, run_id, sequence, event_kind,
                event_payload, event_payload_sha256, created_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (
                submitted_event_id,
                run_id,
                submitted_kind,
                submitted_payload,
                submitted_digest,
                now_text,
            ),
        )
        authorized_event_kind, authorized_event_payload, authorized_event_digest = self.dump_event(
            authorized_event
        )
        conn.execute(
            """
            INSERT INTO scheduler_events(
                event_id, run_id, sequence, event_kind,
                event_payload, event_payload_sha256, created_at
            ) VALUES (?, ?, 2, ?, ?, ?, ?)
            """,
            (
                authorized_event_id,
                run_id,
                authorized_event_kind,
                authorized_event_payload,
                authorized_event_digest,
                now_text,
            ),
        )

    def has_pending_effects_for_run(self, conn: sqlite3.Connection, run_id: str) -> bool:
        row = conn.execute(
            """
            SELECT 1 FROM scheduler_effects
            WHERE run_id = ? AND status = ?
            LIMIT 1
            """,
            (run_id, EFFECT_STATUS_PENDING),
        ).fetchone()
        return row is not None

    def complete_sequence_checkpoint_handoff(
        self,
        conn: sqlite3.Connection,
        *,
        intent: object,
        result: object,
        predecessor_state: object,
        predecessor_version: int,
        successor_submitted: object,
        successor_authorized: object,
        submitted_event: SchedulerEvent,
        authorized_event: SchedulerEvent,
        committed_event: SchedulerEvent,
        handoff_event: SchedulerEvent,
        updated_sequence: object,
        event_id_factory: Callable[[], str],
        now: datetime,
    ) -> None:
        from ai_dev_loop.scheduler.domain.checkpoint import (
            SequenceCheckpointIntent,
            SequenceCheckpointResult,
        )
        from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState
        from ai_dev_loop.scheduler.domain.state import (
            AuthorizedState,
            CompletedState,
            CompletedWithResidualRiskState,
            SubmittedState,
        )

        if not isinstance(intent, SequenceCheckpointIntent):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "handoff requires SequenceCheckpointIntent",
            )
        if not isinstance(result, SequenceCheckpointResult):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "handoff requires SequenceCheckpointResult",
            )
        if not isinstance(predecessor_state, (CompletedState, CompletedWithResidualRiskState)):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "handoff requires terminal predecessor state",
            )
        if not isinstance(successor_submitted, SubmittedState) or not isinstance(
            successor_authorized, AuthorizedState
        ):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "handoff requires successor submitted/authorized states",
            )
        if not isinstance(updated_sequence, ActiveSequenceState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "handoff requires ActiveSequenceState",
            )
        sequence_state = self.load_validated_sequence_state(conn, intent.sequence_id)
        if not isinstance(sequence_state, ActiveSequenceState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence must be active for checkpoint handoff",
            )
        if sequence_state.current_run_id != intent.predecessor_run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence current_run_id must match predecessor",
            )
        if sequence_state.current_ordinal != intent.predecessor_ordinal:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence current_ordinal must match predecessor",
            )
        if sequence_state.version != intent.sequence_version:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence version drift",
            )
        reservation = self.get_reservation_for_run(conn, intent.predecessor_run_id)
        if reservation is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "predecessor must own active reservation",
            )
        existing_successor = conn.execute(
            "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
            (intent.successor_run_id,),
        ).fetchone()
        if existing_successor is not None:
            current, _, _ = self.load_validated_snapshot(conn, intent.successor_run_id)
            if (
                current.kind == "authorized"
                and current.run_id == intent.successor_run_id
                and str(reservation["run_id"]) == intent.successor_run_id
            ):
                if not self.compare_and_swap_state(
                    conn,
                    run_id=intent.predecessor_run_id,
                    expected_version=predecessor_version,
                    new_state=predecessor_state,
                    now=now,
                ):
                    raise SchedulerEngineError(
                        SchedulerEngineErrorKind.CONFLICT,
                        "predecessor CAS lost during idempotent handoff",
                    )
                if not self.compare_and_swap_sequence_state(
                    conn,
                    sequence_id=intent.sequence_id,
                    expected_version=intent.sequence_version,
                    new_state=updated_sequence,
                    now=now,
                ):
                    raise SchedulerEngineError(
                        SchedulerEngineErrorKind.CONFLICT,
                        "sequence CAS lost during idempotent handoff",
                    )
                from ai_dev_loop.scheduler.application.sequence_lineage_ops import (
                    resolve_accepted_phase_handoff,
                )

                successor_entry = updated_sequence.definition.entries[intent.successor_ordinal - 1]
                resolve_accepted_phase_handoff(
                    self,
                    conn,
                    sequence_id=intent.sequence_id,
                    predecessor_ordinal=intent.predecessor_ordinal,
                    predecessor_run_id=intent.predecessor_run_id,
                    accepted_outcome=intent.accepted_outcome,
                    successor_ordinal=intent.successor_ordinal,
                    successor_run_id=intent.successor_run_id,
                    successor_planned_run_id=successor_entry.planned_run_id,
                    successor_materialized_at=encode_utc_instant(now),
                    resolved_at=now,
                )
                self.release_checkpoint_reconciliation_hold(
                    conn,
                    run_id=intent.predecessor_run_id,
                    intent_sha256=result.intent_sha256,
                )
                return
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "successor run already exists with incompatible state",
            )
        submitted_event_id = event_id_factory()
        authorized_event_id = event_id_factory()
        committed_event_id = event_id_factory()
        handoff_event_id = event_id_factory()
        self._insert_handoff_successor_run(
            conn,
            run_id=intent.successor_run_id,
            submitted_state=successor_submitted,
            authorized_state=successor_authorized,
            submitted_event_id=submitted_event_id,
            submitted_event=submitted_event,
            authorized_event_id=authorized_event_id,
            authorized_event=authorized_event,
            now=now,
        )
        predecessor_sequence = self.next_event_sequence(conn, intent.predecessor_run_id)
        self.append_event(
            conn,
            event_id=committed_event_id,
            run_id=intent.predecessor_run_id,
            sequence=predecessor_sequence,
            event=committed_event,
            now=now,
        )
        if not self.compare_and_swap_state(
            conn,
            run_id=intent.predecessor_run_id,
            expected_version=predecessor_version,
            new_state=predecessor_state,
            now=now,
        ):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "predecessor CAS lost during handoff",
            )
        transfer = conn.execute(
            """
            UPDATE scheduler_repository_reservations
            SET run_id = ?, updated_at = ?
            WHERE worktree_key = ? AND run_id = ? AND status = ?
            """,
            (
                intent.successor_run_id,
                encode_utc_instant(now),
                predecessor_state.context.repository.worktree_key,
                intent.predecessor_run_id,
                ReservationStatus.ACTIVE.value,
            ),
        )
        if transfer.rowcount != 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "reservation transfer failed",
            )
        successor_sequence = self.next_event_sequence(conn, intent.successor_run_id)
        self.append_event(
            conn,
            event_id=handoff_event_id,
            run_id=intent.successor_run_id,
            sequence=successor_sequence,
            event=handoff_event,
            now=now,
        )
        if not self.compare_and_swap_sequence_state(
            conn,
            sequence_id=intent.sequence_id,
            expected_version=intent.sequence_version,
            new_state=updated_sequence,
            now=now,
        ):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence CAS lost during handoff",
            )
        from ai_dev_loop.scheduler.application.sequence_lineage_ops import (
            resolve_accepted_phase_handoff,
        )

        successor_entry = updated_sequence.definition.entries[intent.successor_ordinal - 1]
        resolve_accepted_phase_handoff(
            self,
            conn,
            sequence_id=intent.sequence_id,
            predecessor_ordinal=intent.predecessor_ordinal,
            predecessor_run_id=intent.predecessor_run_id,
            accepted_outcome=intent.accepted_outcome,
            successor_ordinal=intent.successor_ordinal,
            successor_run_id=intent.successor_run_id,
            successor_planned_run_id=successor_entry.planned_run_id,
            successor_materialized_at=encode_utc_instant(now),
            resolved_at=now,
        )
        self.release_checkpoint_reconciliation_hold(
            conn,
            run_id=intent.predecessor_run_id,
            intent_sha256=result.intent_sha256,
        )

    def finalize_sequence_state(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_state: object,
        finalized_state: object,
        finalized_event: object,
        event_id_factory: Callable[[], str],
        now: datetime,
    ) -> None:
        from ai_dev_loop.scheduler.domain.sequence import (
            AWAITING_FINALIZATION_SEQUENCE_STATE_KIND,
            ActiveSequenceState,
            AwaitingFinalizationSequenceState,
        )

        if not isinstance(sequence_state, ActiveSequenceState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "finalize requires ActiveSequenceState",
            )
        if not isinstance(finalized_state, AwaitingFinalizationSequenceState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "finalize requires AwaitingFinalizationSequenceState",
            )
        kind, payload, digest = self.dump_sequence_state(finalized_state)
        if kind != AWAITING_FINALIZATION_SEQUENCE_STATE_KIND:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "finalize requires awaiting_finalization sequence state",
            )
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_sequences
            SET state_kind = ?, payload = ?, payload_sha256 = ?,
                version = ?, updated_at = ?
            WHERE sequence_id = ? AND version = ?
            """,
            (
                kind,
                payload,
                digest,
                finalized_state.version,
                now_text,
                sequence_state.sequence_id,
                sequence_state.version,
            ),
        )
        if cursor.rowcount != 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence finalize CAS lost",
            )
        from ai_dev_loop.scheduler.application.sequence_lineage_ops import (
            resolve_sequence_run_terminal,
        )

        resolve_sequence_run_terminal(
            self,
            conn,
            sequence_id=finalized_state.sequence_id,
            ordinal=len(finalized_state.definition.entries),
            run_id=finalized_state.final_run_id,
            terminal_outcome=finalized_state.final_outcome,
            resolved_at=now,
        )

    def get_run_row(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM scheduler_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.NOT_FOUND,
                f"run not found: {run_id}",
            )
        return cast(sqlite3.Row, row)

    def load_validated_snapshot(
        self, conn: sqlite3.Connection, run_id: str
    ) -> tuple[SchedulerState, int, datetime]:
        from ai_dev_loop.scheduler.domain.common import worktree_key
        from ai_dev_loop.scheduler.domain.state import AuthorizedState

        row = self.get_run_row(conn, run_id)
        state = self.load_state(row["state_payload"])
        if state.kind != row["state_kind"]:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "state_kind projection disagrees with payload",
            )
        if payload_sha256(row["state_payload"]) != row["state_payload_sha256"]:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "state payload hash mismatch",
            )
        if state.run_id != run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "state.run_id disagrees with primary key",
            )
        payload_root = str(state.context.repository.root)
        if worktree_key(payload_root) != str(row["worktree_key"]):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "repository identity disagrees with indexed worktree key",
            )
        reservation = self.get_reservation_for_run(conn, run_id)
        if reservation is not None and str(reservation["repository_root"]) != payload_root:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "repository root disagrees with active reservation",
            )
        if isinstance(state, AuthorizedState) and (
            state.authorized_controller_session_id != state.context.controller.controller_session_id
        ):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "controller identity disagrees with frozen context",
            )
        updated_at = datetime.fromisoformat(
            encode_utc_instant(row["updated_at"]).replace("Z", "+00:00")
        )
        return state, int(row["version"]), updated_at

    def compare_and_swap_state(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        expected_version: int,
        new_state: SchedulerState,
        now: datetime,
    ) -> bool:
        kind, payload, digest = self.dump_state(new_state)
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_runs
            SET state_kind = ?, state_payload = ?, state_payload_sha256 = ?,
                version = ?, updated_at = ?
            WHERE run_id = ? AND version = ?
            """,
            (
                kind,
                payload,
                digest,
                new_state.version,
                now_text,
                run_id,
                expected_version,
            ),
        )
        return cursor.rowcount == 1

    def list_runs(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        rows = conn.execute(
            """
            SELECT run_id, state_kind, version, idempotency_key, worktree_key,
                   created_at, updated_at
            FROM scheduler_runs
            ORDER BY created_at ASC, run_id ASC
            """
        ).fetchall()
        return list(rows)

    def next_event_sequence(self, conn: sqlite3.Connection, run_id: str) -> int:
        row = conn.execute(
            "SELECT MAX(sequence) FROM scheduler_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        current = int(row[0]) if row[0] is not None else 0
        return current + 1

    def append_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        run_id: str,
        sequence: int,
        event: SchedulerEvent,
        now: datetime,
    ) -> None:
        event_kind, event_payload, event_digest = self.dump_event(event)
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            INSERT INTO scheduler_events(
                event_id, run_id, sequence, event_kind,
                event_payload, event_payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, run_id, sequence, event_kind, event_payload, event_digest, now_text),
        )

    def get_reservation_for_run(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT * FROM scheduler_repository_reservations
            WHERE run_id = ? AND status = ?
            """,
            (run_id, ReservationStatus.ACTIVE.value),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def release_reservation(
        self,
        conn: sqlite3.Connection,
        *,
        worktree_key: str,
        now: datetime,
    ) -> None:
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            UPDATE scheduler_repository_reservations
            SET status = ?, updated_at = ?
            WHERE worktree_key = ? AND status = ?
            """,
            (
                ReservationStatus.RELEASED.value,
                now_text,
                worktree_key,
                ReservationStatus.ACTIVE.value,
            ),
        )

    def reacquire_released_reservation_for_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        worktree_key: str,
        repository_root: str,
        now: datetime,
    ) -> bool:
        active = self.get_active_reservation(conn, worktree_key)
        if active is not None and str(active["run_id"]) != run_id:
            return False
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_repository_reservations
            SET status = ?, repository_root = ?, updated_at = ?
            WHERE worktree_key = ? AND run_id = ? AND status = ?
            """,
            (
                ReservationStatus.ACTIVE.value,
                repository_root,
                now_text,
                worktree_key,
                run_id,
                ReservationStatus.RELEASED.value,
            ),
        )
        if cursor.rowcount == 1:
            return True
        cursor = conn.execute(
            """
            INSERT INTO scheduler_repository_reservations(
                worktree_key, run_id, repository_root, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(worktree_key) DO UPDATE SET
                run_id = excluded.run_id,
                repository_root = excluded.repository_root,
                status = excluded.status,
                updated_at = excluded.updated_at
            WHERE scheduler_repository_reservations.status = 'released'
              AND scheduler_repository_reservations.run_id = excluded.run_id
            """,
            (
                worktree_key,
                run_id,
                repository_root,
                ReservationStatus.ACTIVE.value,
                now_text,
                now_text,
            ),
        )
        return cursor.rowcount == 1

    def list_tick_eligible_run_ids(self, conn: sqlite3.Connection) -> list[str]:
        placeholders = ",".join("?" * len(TICK_ELIGIBLE_STATE_KINDS))
        rows = conn.execute(
            f"""
            SELECT run_id FROM scheduler_runs
            WHERE state_kind IN ({placeholders})
            ORDER BY created_at ASC, run_id ASC
            """,
            tuple(TICK_ELIGIBLE_STATE_KINDS),
        ).fetchall()
        run_ids = [str(row[0]) for row in rows]
        for run_id in self.list_aborted_reconcile_run_ids(conn):
            if run_id not in run_ids:
                run_ids.append(run_id)
        return run_ids

    def find_runs_for_controller(
        self,
        conn: sqlite3.Connection,
        *,
        controller_session_id: str,
        repository_root: str,
        include_terminal: bool = False,
    ) -> list[sqlite3.Row]:
        from ai_dev_loop.scheduler.domain.common import worktree_key

        wt_key = worktree_key(repository_root)
        if include_terminal:
            rows = conn.execute(
                """
                SELECT run_id, state_kind, state_payload, version, created_at, updated_at
                FROM scheduler_runs
                WHERE worktree_key = ?
                ORDER BY created_at ASC, run_id ASC
                """,
                (wt_key,),
            ).fetchall()
        else:
            placeholders = ",".join("?" * len(NON_TERMINAL_STATE_KINDS))
            rows = conn.execute(
                f"""
                SELECT run_id, state_kind, state_payload, version, created_at, updated_at
                FROM scheduler_runs
                WHERE worktree_key = ?
                  AND state_kind IN ({placeholders})
                ORDER BY created_at ASC, run_id ASC
                """,
                (wt_key, *NON_TERMINAL_STATE_KINDS),
            ).fetchall()
        matches: list[sqlite3.Row] = []
        for row in rows:
            db_run_id = str(row["run_id"])
            state, _, _ = self.load_validated_snapshot(conn, db_run_id)
            if state.context.controller.controller_session_id != controller_session_id:
                continue
            if str(state.context.repository.root) != repository_root:
                continue
            matches.append(row)
        return matches

    def get_tick_lease_row(self, conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM scheduler_tick_leases WHERE lease_name = ?",
            (TICK_LEASE_NAME,),
        ).fetchone()
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "global tick lease row missing",
            )
        return cast(sqlite3.Row, row)

    def acquire_global_tick_lease(
        self,
        conn: sqlite3.Connection,
        *,
        owner_id: str,
        now: datetime,
        ttl_seconds: int,
    ) -> tuple[int, datetime] | None:
        row = self.get_tick_lease_row(conn)
        expires_at = (
            datetime.fromisoformat(encode_utc_instant(row["expires_at"]).replace("Z", "+00:00"))
            if row["expires_at"] is not None
            else None
        )
        if (
            row["status"] == "active"
            and expires_at is not None
            and expires_at > now
            and row["owner_id"] != owner_id
        ):
            return None
        generation = int(row["generation"]) + 1
        lease_expires = now + timedelta(seconds=ttl_seconds)
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            UPDATE scheduler_tick_leases
            SET owner_id = ?, generation = ?, status = 'active',
                acquired_at = ?, expires_at = ?, updated_at = ?
            WHERE lease_name = ?
            """,
            (
                owner_id,
                generation,
                now_text,
                encode_utc_instant(lease_expires),
                now_text,
                TICK_LEASE_NAME,
            ),
        )
        return generation, lease_expires

    def release_global_tick_lease(
        self,
        conn: sqlite3.Connection,
        *,
        owner_id: str,
        generation: int,
        now: datetime,
    ) -> bool:
        row = self.get_tick_lease_row(conn)
        if row["owner_id"] != owner_id or int(row["generation"]) != generation:
            return False
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            UPDATE scheduler_tick_leases
            SET status = 'inactive', owner_id = NULL, acquired_at = NULL,
                expires_at = NULL, updated_at = ?
            WHERE lease_name = ? AND owner_id = ? AND generation = ?
            """,
            (now_text, TICK_LEASE_NAME, owner_id, generation),
        )
        return True

    def reconcile_stale_tick_resources(
        self,
        conn: sqlite3.Connection,
        *,
        current_generation: int,
        now: datetime,
    ) -> None:
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            UPDATE scheduler_run_tick_claims
            SET status = 'stale', released_at = ?, updated_at = ?
            WHERE status = 'active' AND tick_lease_generation < ?
            """,
            (now_text, now_text, current_generation),
        )
        conn.execute(
            """
            UPDATE scheduler_capacity
            SET holder_run_id = NULL, holder_claim_id = NULL,
                holder_tick_generation = NULL, updated_at = ?
            WHERE capacity_name = ? AND holder_tick_generation IS NOT NULL
              AND holder_tick_generation < ?
              AND NOT EXISTS (
                  SELECT 1 FROM scheduler_attempts
                  WHERE scheduler_attempts.capacity_claim_id = scheduler_capacity.holder_claim_id
                    AND (
                      scheduler_attempts.status IN ('launching', 'active', 'uncertain')
                      OR (
                        scheduler_attempts.status = 'cancelled'
                        AND scheduler_attempts.completion_fence_id IS NOT NULL
                        AND scheduler_attempts.ingested = 0
                      )
                    )
              )
            """,
            (now_text, CAPACITY_NAME, current_generation),
        )
        conn.execute(
            """
            UPDATE scheduler_effects
            SET status = ?, updated_at = ?
            WHERE status = ? AND claim_lease_generation IS NOT NULL
              AND claim_lease_generation < ?
              AND NOT EXISTS (
                  SELECT 1 FROM scheduler_attempts
                  WHERE scheduler_attempts.dispatch_id = scheduler_effects.dispatch_id
                    AND (
                      scheduler_attempts.status IN ('launching', 'active', 'uncertain')
                      OR (
                        scheduler_attempts.status = 'cancelled'
                        AND scheduler_attempts.completion_fence_id IS NOT NULL
                        AND scheduler_attempts.ingested = 0
                      )
                    )
              )
            """,
            (
                EFFECT_STATUS_SUPERSEDED,
                now_text,
                EFFECT_STATUS_CLAIMED,
                current_generation,
            ),
        )
        conn.execute(
            """
            UPDATE scheduler_claims
            SET status = 'stale', released_at = ?, updated_at = ?
            WHERE status = 'active' AND lease_generation < ?
              AND NOT EXISTS (
                  SELECT 1 FROM scheduler_attempts
                  WHERE scheduler_attempts.capacity_claim_id = scheduler_claims.claim_id
                    AND scheduler_attempts.status IN ('launching', 'active', 'uncertain')
              )
            """,
            (now_text, now_text, current_generation),
        )

    def has_stale_admission_attempt(self, conn: sqlite3.Connection, run_id: str) -> bool:
        row = conn.execute(
            """
            SELECT 1 FROM scheduler_run_tick_claims
            WHERE run_id = ? AND purpose = 'admission' AND status = 'stale'
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return row is not None

    def get_active_admission_claim_for_run(
        self, conn: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT * FROM scheduler_run_tick_claims
            WHERE run_id = ? AND purpose = 'admission' AND status = 'active'
            """,
            (run_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def acquire_admission_tick_claim(
        self,
        conn: sqlite3.Connection,
        *,
        claim_id: str,
        run_id: str,
        tick_owner_id: str,
        tick_lease_generation: int,
        expected_run_version: int,
        now: datetime,
    ) -> bool:
        self.reconcile_stale_tick_resources(
            conn,
            current_generation=tick_lease_generation,
            now=now,
        )
        existing = self.get_active_admission_claim_for_run(conn, run_id)
        if existing is not None:
            if (
                str(existing["tick_owner_id"]) == tick_owner_id
                and int(existing["tick_lease_generation"]) == tick_lease_generation
            ):
                return False
            self.release_admission_tick_claim(
                conn,
                claim_id=str(existing["claim_id"]),
                now=now,
                stale=True,
                owner_id=str(existing["tick_owner_id"]),
                lease_generation=int(existing["tick_lease_generation"]),
            )
        now_text = encode_utc_instant(now)
        try:
            conn.execute(
                """
                INSERT INTO scheduler_run_tick_claims(
                    claim_id, run_id, tick_owner_id, tick_lease_generation,
                    purpose, expected_run_version, status, acquired_at, updated_at
                ) VALUES (?, ?, ?, ?, 'admission', ?, 'active', ?, ?)
                """,
                (
                    claim_id,
                    run_id,
                    tick_owner_id,
                    tick_lease_generation,
                    expected_run_version,
                    now_text,
                    now_text,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def release_admission_tick_claim(
        self,
        conn: sqlite3.Connection,
        *,
        claim_id: str,
        now: datetime,
        stale: bool = False,
        owner_id: str | None = None,
        lease_generation: int | None = None,
    ) -> bool:
        status = "stale" if stale else "released"
        now_text = encode_utc_instant(now)
        if owner_id is not None and lease_generation is not None:
            cursor = conn.execute(
                """
                UPDATE scheduler_run_tick_claims
                SET status = ?, released_at = ?, updated_at = ?
                WHERE claim_id = ? AND status = 'active'
                  AND tick_owner_id = ? AND tick_lease_generation = ?
                """,
                (status, now_text, now_text, claim_id, owner_id, lease_generation),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE scheduler_run_tick_claims
                SET status = ?, released_at = ?, updated_at = ?
                WHERE claim_id = ? AND status = 'active'
                """,
                (status, now_text, now_text, claim_id),
            )
        return cursor.rowcount == 1

    def get_active_admission_claim(
        self, conn: sqlite3.Connection, claim_id: str
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT * FROM scheduler_run_tick_claims
            WHERE claim_id = ? AND status = 'active' AND purpose = 'admission'
            """,
            (claim_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def insert_synthetic_self_test_effect(
        self,
        conn: sqlite3.Connection,
        *,
        dispatch_id: str,
        source_event_id: str,
        run_id: str,
        available_at: datetime,
        claimed_run_version: int,
        now: datetime,
    ) -> None:
        payload = json.dumps(
            {"effect_kind": SYNTHETIC_SELF_TEST_EFFECT_KIND},
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = payload_sha256(payload)
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            INSERT INTO scheduler_effects(
                dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                status, available_at, claimed_run_version, created_at, updated_at
            ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dispatch_id,
                source_event_id,
                run_id,
                SYNTHETIC_SELF_TEST_EFFECT_ID,
                f"{run_id}:{SYNTHETIC_SELF_TEST_EFFECT_ID}",
                SYNTHETIC_SELF_TEST_EFFECT_KIND,
                payload,
                digest,
                EFFECT_STATUS_PENDING,
                encode_utc_instant(available_at),
                claimed_run_version,
                now_text,
                now_text,
            ),
        )

    def insert_fake_agent_self_test_effect(
        self,
        conn: sqlite3.Connection,
        *,
        dispatch_id: str,
        source_event_id: str,
        run_id: str,
        available_at: datetime,
        claimed_run_version: int,
        now: datetime,
    ) -> None:
        payload = json.dumps(
            {"effect_kind": FAKE_AGENT_SELF_TEST_EFFECT_KIND},
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = payload_sha256(payload)
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            INSERT INTO scheduler_effects(
                dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                status, available_at, claimed_run_version, created_at, updated_at
            ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dispatch_id,
                source_event_id,
                run_id,
                FAKE_AGENT_SELF_TEST_EFFECT_ID,
                f"{run_id}:{FAKE_AGENT_SELF_TEST_EFFECT_ID}",
                FAKE_AGENT_SELF_TEST_EFFECT_KIND,
                payload,
                digest,
                EFFECT_STATUS_PENDING,
                encode_utc_instant(available_at),
                claimed_run_version,
                now_text,
                now_text,
            ),
        )

    def list_eligible_effects(
        self, conn: sqlite3.Connection, *, run_id: str, now: datetime
    ) -> list[sqlite3.Row]:
        now_text = encode_utc_instant(now)
        rows = conn.execute(
            """
            SELECT * FROM scheduler_effects
            WHERE run_id = ? AND status = ? AND available_at <= ?
            ORDER BY created_at ASC, dispatch_id ASC
            """,
            (run_id, EFFECT_STATUS_PENDING, now_text),
        ).fetchall()
        return list(rows)

    def claim_effect(
        self,
        conn: sqlite3.Connection,
        *,
        dispatch_id: str,
        claim_id: str,
        tick_owner_id: str,
        tick_lease_generation: int,
        expected_run_version: int,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_effects
            SET status = ?, claim_id = ?, claim_owner_id = ?, claim_lease_generation = ?,
                claimed_run_version = ?, claimed_at = ?, updated_at = ?
            WHERE dispatch_id = ? AND status = ?
            """,
            (
                EFFECT_STATUS_CLAIMED,
                claim_id,
                tick_owner_id,
                tick_lease_generation,
                expected_run_version,
                now_text,
                now_text,
                dispatch_id,
                EFFECT_STATUS_PENDING,
            ),
        )
        if cursor.rowcount != 1:
            return False
        conn.execute(
            """
            INSERT INTO scheduler_claims(
                claim_id, run_id, dispatch_id, owner_id, lease_generation,
                status, acquired_at, updated_at
            ) SELECT ?, run_id, dispatch_id, ?, ?, 'active', ?, ?
            FROM scheduler_effects WHERE dispatch_id = ?
            """,
            (claim_id, tick_owner_id, tick_lease_generation, now_text, now_text, dispatch_id),
        )
        return True

    def get_claimed_effect_row(
        self, conn: sqlite3.Connection, dispatch_id: str
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT * FROM scheduler_effects
            WHERE dispatch_id = ? AND status = ?
            """,
            (dispatch_id, EFFECT_STATUS_CLAIMED),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def complete_claimed_effect(
        self,
        conn: sqlite3.Connection,
        *,
        dispatch_id: str,
        claim_id: str,
        tick_owner_id: str,
        tick_lease_generation: int,
        expected_run_version: int,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_effects
            SET status = ?, completed_at = ?, updated_at = ?
            WHERE dispatch_id = ? AND claim_id = ? AND status = ?
              AND claim_owner_id = ? AND claim_lease_generation = ?
              AND claimed_run_version = ?
              AND EXISTS (
                  SELECT 1 FROM scheduler_runs
                  WHERE scheduler_runs.run_id = scheduler_effects.run_id
                    AND scheduler_runs.version = ?
              )
            """,
            (
                EFFECT_STATUS_SUCCEEDED,
                now_text,
                now_text,
                dispatch_id,
                claim_id,
                EFFECT_STATUS_CLAIMED,
                tick_owner_id,
                tick_lease_generation,
                expected_run_version,
                expected_run_version,
            ),
        )
        if cursor.rowcount != 1:
            return False
        conn.execute(
            """
            UPDATE scheduler_claims
            SET status = 'released', released_at = ?, updated_at = ?
            WHERE claim_id = ? AND status = 'active'
            """,
            (now_text, now_text, claim_id),
        )
        return True

    def mark_effect_stale(
        self,
        conn: sqlite3.Connection,
        *,
        dispatch_id: str,
        claim_id: str,
        now: datetime,
    ) -> None:
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            UPDATE scheduler_effects
            SET status = ?, updated_at = ?
            WHERE dispatch_id = ? AND claim_id = ?
            """,
            (EFFECT_STATUS_SUPERSEDED, now_text, dispatch_id, claim_id),
        )
        conn.execute(
            """
            UPDATE scheduler_claims
            SET status = 'stale', released_at = ?, updated_at = ?
            WHERE claim_id = ?
            """,
            (now_text, now_text, claim_id),
        )

    def try_acquire_capacity(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        claim_id: str,
        tick_owner_id: str,
        tick_lease_generation: int,
        now: datetime,
    ) -> bool:
        row = conn.execute(
            "SELECT * FROM scheduler_capacity WHERE capacity_name = ?",
            (CAPACITY_NAME,),
        ).fetchone()
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "global capacity row missing",
            )
        if row["holder_run_id"] is not None:
            holder_generation = row["holder_tick_generation"]
            if holder_generation is not None and int(holder_generation) < tick_lease_generation:
                self.reconcile_stale_tick_resources(
                    conn,
                    current_generation=tick_lease_generation,
                    now=now,
                )
                row = conn.execute(
                    "SELECT * FROM scheduler_capacity WHERE capacity_name = ?",
                    (CAPACITY_NAME,),
                ).fetchone()
                assert row is not None
            if row["holder_run_id"] is not None:
                return False
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_capacity
            SET holder_run_id = ?, holder_claim_id = ?, holder_tick_generation = ?,
                updated_at = ?
            WHERE capacity_name = ? AND holder_run_id IS NULL
            """,
            (run_id, claim_id, tick_lease_generation, now_text, CAPACITY_NAME),
        )
        return cursor.rowcount == 1

    def release_capacity(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        claim_id: str,
        tick_owner_id: str,
        tick_lease_generation: int,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_capacity
            SET holder_run_id = NULL, holder_claim_id = NULL,
                holder_tick_generation = NULL, updated_at = ?
            WHERE capacity_name = ? AND holder_run_id = ?
              AND holder_claim_id = ? AND holder_tick_generation = ?
            """,
            (now_text, CAPACITY_NAME, run_id, claim_id, tick_lease_generation),
        )
        return cursor.rowcount == 1

    def get_capacity_row(self, conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM scheduler_capacity WHERE capacity_name = ?",
            (CAPACITY_NAME,),
        ).fetchone()
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "global capacity row missing",
            )
        return cast(sqlite3.Row, row)

    def insert_retry_timer(
        self,
        conn: sqlite3.Connection,
        *,
        timer_id: str,
        source_event_id: str,
        run_id: str,
        due_at: datetime,
        target_effect_id: str,
        expected_run_version: int,
        now: datetime,
    ) -> None:
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            INSERT INTO scheduler_timers(
                timer_id, source_event_id, run_id, timer_kind, due_at,
                target_effect_id, expected_run_version, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'retry_due', ?, ?, ?, ?, ?, ?)
            """,
            (
                timer_id,
                source_event_id,
                run_id,
                encode_utc_instant(due_at),
                target_effect_id,
                expected_run_version,
                TIMER_STATUS_PENDING,
                now_text,
                now_text,
            ),
        )

    def list_due_timers(self, conn: sqlite3.Connection, *, now: datetime) -> list[sqlite3.Row]:
        now_text = encode_utc_instant(now)
        rows = conn.execute(
            """
            SELECT * FROM scheduler_timers
            WHERE status = ? AND due_at <= ?
            ORDER BY due_at ASC, timer_id ASC
            """,
            (TIMER_STATUS_PENDING, now_text),
        ).fetchall()
        return list(rows)

    def fire_timer(
        self,
        conn: sqlite3.Connection,
        *,
        timer_id: str,
        fired_event_id: str,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_timers
            SET status = ?, fired_event_id = ?, updated_at = ?
            WHERE timer_id = ? AND status = ?
            """,
            (TIMER_STATUS_FIRED, fired_event_id, now_text, timer_id, TIMER_STATUS_PENDING),
        )
        return cursor.rowcount == 1

    def get_fired_retry_timer_for_run(
        self,
        conn: sqlite3.Connection,
        run_id: str,
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT * FROM scheduler_timers
            WHERE run_id = ? AND timer_kind = 'retry_due' AND status = ?
            ORDER BY due_at DESC, timer_id DESC
            LIMIT 1
            """,
            (run_id, TIMER_STATUS_FIRED),
        ).fetchone()
        return cast(sqlite3.Row, row) if row is not None else None

    def get_cancelled_reconcile_attempt_for_run(
        self, conn: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT * FROM scheduler_attempts
            WHERE run_id = ? AND status = ?
              AND completion_fence_id IS NOT NULL
              AND ingested = 0
            ORDER BY created_at DESC, attempt_id DESC
            LIMIT 1
            """,
            (run_id, ATTEMPT_STATUS_CANCELLED),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    @staticmethod
    def schema_supports_checkpoint_holds(conn: sqlite3.Connection) -> bool:
        return SqliteSchedulerStore._user_version(conn) >= CHECKPOINT_HOLD_SCHEMA_VERSION

    def has_abort_requested_for_run(self, conn: sqlite3.Connection, run_id: str) -> bool:
        row = conn.execute(
            """
            SELECT 1 FROM scheduler_events
            WHERE run_id = ? AND event_kind = 'abort_requested'
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return row is not None

    def has_sequence_abort_requested_for_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        sequence_id: str,
    ) -> bool:
        row = conn.execute(
            """
            SELECT 1 FROM scheduler_events
            WHERE run_id = ?
              AND event_kind = 'sequence_abort_requested'
              AND json_extract(event_payload, '$.sequence_id') = ?
            LIMIT 1
            """,
            (run_id, sequence_id),
        ).fetchone()
        return row is not None

    def list_reconcilable_sequence_ids(self, conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            """
            SELECT sequence_id
            FROM scheduler_sequences
            WHERE state_kind IN ('active', 'abort_pending')
            ORDER BY prepared_at ASC, sequence_id ASC
            """
        ).fetchall()
        return [str(row["sequence_id"]) for row in rows]

    def list_sequences_pending_report_publication(self, conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            """
            SELECT sequence_id
            FROM scheduler_sequences
            WHERE state_kind = 'awaiting_finalization'
              AND (
                json_extract(payload, '$.completion_report_sha256') IS NULL
                OR json_extract(payload, '$.completion_report_sha256') = ''
              )
            ORDER BY prepared_at ASC, sequence_id ASC
            """
        ).fetchall()
        return [str(row["sequence_id"]) for row in rows]

    def list_abort_pending_sequence_run_ids(self, conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            """
            SELECT json_extract(payload, '$.current_run_id') AS run_id
            FROM scheduler_sequences
            WHERE state_kind = 'abort_pending'
            ORDER BY prepared_at ASC, sequence_id ASC
            """
        ).fetchall()
        return [str(row["run_id"]) for row in rows if row["run_id"] is not None]

    def get_checkpoint_reconciliation_hold_row(
        self, conn: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row | None:
        if not self.schema_supports_checkpoint_holds(conn):
            return None
        row = conn.execute(
            """
            SELECT run_id, intent_sha256, hold_reason, ref_may_have_advanced
            FROM scheduler_checkpoint_holds
            WHERE run_id = ?
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def has_checkpoint_reconciliation_hold(self, conn: sqlite3.Connection, run_id: str) -> bool:
        return self.get_checkpoint_reconciliation_hold_row(conn, run_id) is not None

    def acquire_checkpoint_reconciliation_hold(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        intent_sha256: str,
        hold_reason: str,
        ref_may_have_advanced: bool,
        now: datetime,
    ) -> None:
        if not self.schema_supports_checkpoint_holds(conn):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                "checkpoint reconciliation holds require scheduler schema version 7",
            )
        now_text = encode_utc_instant(now)
        ref_flag = 1 if ref_may_have_advanced else 0
        conn.execute(
            """
            INSERT INTO scheduler_checkpoint_holds(
                run_id, intent_sha256, hold_reason, ref_may_have_advanced,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                intent_sha256 = excluded.intent_sha256,
                hold_reason = excluded.hold_reason,
                ref_may_have_advanced = MAX(
                    scheduler_checkpoint_holds.ref_may_have_advanced,
                    excluded.ref_may_have_advanced
                ),
                updated_at = excluded.updated_at
            WHERE scheduler_checkpoint_holds.intent_sha256 = excluded.intent_sha256
            """,
            (run_id, intent_sha256, hold_reason, ref_flag, now_text, now_text),
        )
        if conn.total_changes == 0:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "checkpoint reconciliation hold intent mismatch",
            )

    def release_checkpoint_reconciliation_hold(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        intent_sha256: str,
    ) -> None:
        if not self.schema_supports_checkpoint_holds(conn):
            return
        conn.execute(
            """
            DELETE FROM scheduler_checkpoint_holds
            WHERE run_id = ? AND intent_sha256 = ?
            """,
            (run_id, intent_sha256),
        )

    def has_unresolved_abort_hold(self, conn: sqlite3.Connection, run_id: str) -> bool:
        if self.schema_supports_checkpoint_holds(conn) and self.has_checkpoint_reconciliation_hold(
            conn, run_id
        ):
            return True
        row = conn.execute(
            """
            SELECT 1 FROM scheduler_attempts
            WHERE run_id = ?
              AND (
                status IN (?, ?, ?)
                OR (
                  status = ?
                  AND completion_fence_id IS NOT NULL
                  AND ingested = 0
                )
              )
            LIMIT 1
            """,
            (
                run_id,
                ATTEMPT_STATUS_LAUNCHING,
                ATTEMPT_STATUS_ACTIVE,
                ATTEMPT_STATUS_UNCERTAIN,
                ATTEMPT_STATUS_CANCELLED,
            ),
        ).fetchone()
        return row is not None

    def has_unreleased_abort_resources(self, conn: sqlite3.Connection, run_id: str) -> bool:
        capacity = self.get_capacity_row(conn)
        if capacity["holder_run_id"] is not None and str(capacity["holder_run_id"]) == run_id:
            return True
        return self.get_reservation_for_run(conn, run_id) is not None

    def list_pending_abort_attempts(
        self, conn: sqlite3.Connection, run_id: str
    ) -> list[sqlite3.Row]:
        rows = conn.execute(
            """
            SELECT * FROM scheduler_attempts
            WHERE run_id = ?
              AND (
                status IN (?, ?, ?)
                OR (
                  status = ?
                  AND completion_fence_id IS NOT NULL
                  AND ingested = 0
                )
              )
            ORDER BY created_at ASC, attempt_id ASC
            """,
            (
                run_id,
                ATTEMPT_STATUS_LAUNCHING,
                ATTEMPT_STATUS_ACTIVE,
                ATTEMPT_STATUS_UNCERTAIN,
                ATTEMPT_STATUS_CANCELLED,
            ),
        ).fetchall()
        return [cast(sqlite3.Row, row) for row in rows]

    def list_aborted_reconcile_run_ids(self, conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            """
            SELECT DISTINCT sr.run_id
            FROM scheduler_runs sr
            WHERE sr.state_kind = 'aborted'
              AND (
                sr.run_id IN (
                    SELECT run_id FROM scheduler_attempts
                    WHERE status IN (?, ?, ?)
                       OR (
                         status = ?
                         AND completion_fence_id IS NOT NULL
                         AND ingested = 0
                       )
                )
                OR (
                  NOT EXISTS (
                      SELECT 1 FROM scheduler_attempts
                      WHERE run_id = sr.run_id
                        AND (
                          status IN (?, ?, ?)
                          OR (
                            status = ?
                            AND completion_fence_id IS NOT NULL
                            AND ingested = 0
                          )
                        )
                  )
                  AND (
                    EXISTS (
                        SELECT 1 FROM scheduler_capacity
                        WHERE holder_run_id = sr.run_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM scheduler_repository_reservations
                        WHERE run_id = sr.run_id AND status = ?
                    )
                  )
                )
              )
            ORDER BY sr.created_at ASC, sr.run_id ASC
            """,
            (
                ATTEMPT_STATUS_LAUNCHING,
                ATTEMPT_STATUS_ACTIVE,
                ATTEMPT_STATUS_UNCERTAIN,
                ATTEMPT_STATUS_CANCELLED,
                ATTEMPT_STATUS_LAUNCHING,
                ATTEMPT_STATUS_ACTIVE,
                ATTEMPT_STATUS_UNCERTAIN,
                ATTEMPT_STATUS_CANCELLED,
                ReservationStatus.ACTIVE.value,
            ),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def get_nonterminal_attempt_for_run(
        self, conn: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row | None:
        placeholders = ",".join("?" * len(NON_TERMINAL_ATTEMPT_STATUSES))
        row = conn.execute(
            f"""
            SELECT * FROM scheduler_attempts
            WHERE run_id = ? AND status IN ({placeholders})
            ORDER BY created_at ASC, attempt_id ASC
            LIMIT 1
            """,
            (run_id, *NON_TERMINAL_ATTEMPT_STATUSES),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def get_attempt_by_id(self, conn: sqlite3.Connection, attempt_id: str) -> sqlite3.Row | None:
        row = conn.execute(
            "SELECT * FROM scheduler_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def insert_launch_requested_attempt(
        self,
        conn: sqlite3.Connection,
        *,
        attempt_id: str,
        run_id: str,
        dispatch_id: str,
        launch_nonce: str,
        unit_identity: str,
        launch_intent_sha256: str,
        capacity_claim_id: str,
        capacity_tick_generation: int,
        stdout_artifact_path: str,
        stderr_artifact_path: str,
        result_artifact_path: str,
        now: datetime,
        component: str = "cursor",
        iteration: int = 1,
    ) -> None:
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            INSERT INTO scheduler_attempts(
                attempt_id, run_id, dispatch_id, component, iteration, status,
                backend_identity, launch_intent_sha256, stdout_artifact_path,
                stderr_artifact_path, completion_envelope_sha256, created_at, updated_at,
                launch_nonce, unit_identity, capacity_claim_id, capacity_tick_generation,
                result_artifact_path, launch_requested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                run_id,
                dispatch_id,
                component,
                iteration,
                ATTEMPT_STATUS_LAUNCHING,
                unit_identity,
                launch_intent_sha256,
                stdout_artifact_path,
                stderr_artifact_path,
                now_text,
                now_text,
                launch_nonce,
                unit_identity,
                capacity_claim_id,
                capacity_tick_generation,
                result_artifact_path,
                now_text,
            ),
        )

    def get_claim_owner_id(self, conn: sqlite3.Connection, claim_id: str) -> str | None:
        row = conn.execute(
            "SELECT owner_id FROM scheduler_claims WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row["owner_id"])

    def verify_launch_authority(
        self,
        conn: sqlite3.Connection,
        *,
        attempt_id: str,
        run_id: str,
        claim_id: str,
        reconciliation_owner_id: str,
        reconciliation_lease_generation: int,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        row = conn.execute(
            """
            SELECT 1 FROM scheduler_attempts
            WHERE attempt_id = ? AND run_id = ? AND status = ?
              AND capacity_claim_id = ?
              AND launch_intent_sha256 IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM scheduler_tick_leases
                  WHERE lease_name = ? AND status = 'active'
                    AND owner_id = ? AND generation = ?
                    AND expires_at > ?
              )
              AND EXISTS (
                  SELECT 1 FROM scheduler_capacity
                  WHERE capacity_name = ?
                    AND holder_run_id = scheduler_attempts.run_id
                    AND holder_claim_id = scheduler_attempts.capacity_claim_id
                    AND holder_tick_generation = scheduler_attempts.capacity_tick_generation
              )
            """,
            (
                attempt_id,
                run_id,
                ATTEMPT_STATUS_LAUNCHING,
                claim_id,
                TICK_LEASE_NAME,
                reconciliation_owner_id,
                reconciliation_lease_generation,
                now_text,
                CAPACITY_NAME,
            ),
        ).fetchone()
        return row is not None

    def mark_attempt_active(
        self,
        conn: sqlite3.Connection,
        *,
        attempt_id: str,
        reconciliation_owner_id: str,
        reconciliation_lease_generation: int,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_attempts
            SET status = ?, updated_at = ?
            WHERE attempt_id = ? AND status IN (?, ?)
              AND launch_intent_sha256 IS NOT NULL
              AND capacity_claim_id IS NOT NULL
              AND capacity_tick_generation IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM scheduler_tick_leases
                  WHERE lease_name = ? AND status = 'active'
                    AND owner_id = ? AND generation = ?
                    AND expires_at > ?
              )
              AND EXISTS (
                  SELECT 1 FROM scheduler_capacity
                  WHERE capacity_name = ?
                    AND holder_run_id = scheduler_attempts.run_id
                    AND holder_claim_id = scheduler_attempts.capacity_claim_id
                    AND holder_tick_generation = scheduler_attempts.capacity_tick_generation
              )
            """,
            (
                ATTEMPT_STATUS_ACTIVE,
                now_text,
                attempt_id,
                ATTEMPT_STATUS_LAUNCHING,
                ATTEMPT_STATUS_UNCERTAIN,
                TICK_LEASE_NAME,
                reconciliation_owner_id,
                reconciliation_lease_generation,
                now_text,
                CAPACITY_NAME,
            ),
        )
        return cursor.rowcount == 1

    def complete_attempt_fenced(
        self,
        conn: sqlite3.Connection,
        *,
        attempt_id: str,
        dispatch_id: str,
        claim_id: str,
        capacity_claim_owner_id: str,
        capacity_tick_generation: int,
        expected_run_version: int,
        completion_fence_id: str,
        exit_code: int,
        termination_class: str,
        completion_envelope_sha256: str,
        terminal_status: str,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_attempts
            SET status = ?, exit_code = ?, termination_class = ?,
                completion_envelope_sha256 = ?, completion_fence_id = ?,
                completed_at = ?, updated_at = ?
            WHERE attempt_id = ? AND status IN (?, ?, ?)
              AND capacity_claim_id = ?
              AND capacity_tick_generation = ?
              AND completion_fence_id IS NULL
              AND EXISTS (
                  SELECT 1 FROM scheduler_effects
                  WHERE scheduler_effects.dispatch_id = scheduler_attempts.dispatch_id
                    AND scheduler_effects.dispatch_id = ?
                    AND scheduler_effects.claim_id = scheduler_attempts.capacity_claim_id
                    AND scheduler_effects.claim_id = ?
                    AND scheduler_effects.status = ?
                    AND scheduler_effects.claim_owner_id = ?
                    AND scheduler_effects.claim_lease_generation = ?
                    AND scheduler_effects.claimed_run_version = ?
              )
            """,
            (
                terminal_status,
                exit_code,
                termination_class,
                completion_envelope_sha256,
                completion_fence_id,
                now_text,
                now_text,
                attempt_id,
                ATTEMPT_STATUS_LAUNCHING,
                ATTEMPT_STATUS_ACTIVE,
                ATTEMPT_STATUS_UNCERTAIN,
                claim_id,
                capacity_tick_generation,
                dispatch_id,
                claim_id,
                EFFECT_STATUS_CLAIMED,
                capacity_claim_owner_id,
                capacity_tick_generation,
                expected_run_version,
            ),
        )
        return cursor.rowcount == 1

    def mark_attempt_uncertain(
        self,
        conn: sqlite3.Connection,
        *,
        attempt_id: str,
        claim_id: str,
        tick_lease_generation: int,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_attempts
            SET status = ?, updated_at = ?
            WHERE attempt_id = ? AND status IN (?, ?)
              AND capacity_claim_id = ?
              AND capacity_tick_generation = ?
              AND completion_fence_id IS NULL
            """,
            (
                ATTEMPT_STATUS_UNCERTAIN,
                now_text,
                attempt_id,
                ATTEMPT_STATUS_LAUNCHING,
                ATTEMPT_STATUS_ACTIVE,
                claim_id,
                tick_lease_generation,
            ),
        )
        return cursor.rowcount == 1

    def settle_prelaunch_guard_failure(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        attempt_id: str,
        dispatch_id: str,
        claim_id: str,
        capacity_claim_owner_id: str,
        capacity_tick_generation: int,
        now: datetime,
    ) -> bool:
        """Abort a never-launched attempt and release its claimed effect and capacity."""
        from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass

        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_attempts
            SET status = ?, exit_code = ?, termination_class = ?,
                ingested = 1, completed_at = ?, updated_at = ?
            WHERE attempt_id = ? AND run_id = ? AND status = ?
              AND capacity_claim_id = ?
              AND capacity_tick_generation = ?
            """,
            (
                ATTEMPT_STATUS_FAILED,
                1,
                TerminationClass.NONZERO_EXIT.value,
                now_text,
                now_text,
                attempt_id,
                run_id,
                ATTEMPT_STATUS_LAUNCHING,
                claim_id,
                capacity_tick_generation,
            ),
        )
        if cursor.rowcount != 1:
            return False
        self.mark_effect_stale(
            conn,
            dispatch_id=dispatch_id,
            claim_id=claim_id,
            now=now,
        )
        return self.release_capacity(
            conn,
            run_id=run_id,
            claim_id=claim_id,
            tick_owner_id=capacity_claim_owner_id,
            tick_lease_generation=capacity_tick_generation,
            now=now,
        )

    def insert_effect(
        self,
        conn: sqlite3.Connection,
        *,
        dispatch_id: str,
        source_event_id: str,
        run_id: str,
        effect_id: str,
        effect_kind: str,
        effect_payload: dict[str, object],
        available_at: datetime,
        claimed_run_version: int,
        now: datetime,
    ) -> None:
        payload = json.dumps(effect_payload, sort_keys=True, separators=(",", ":"))
        digest = payload_sha256(payload)
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            INSERT INTO scheduler_effects(
                dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                status, available_at, claimed_run_version, created_at, updated_at
            ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dispatch_id,
                source_event_id,
                run_id,
                effect_id,
                f"{run_id}:{effect_id}",
                effect_kind,
                payload,
                digest,
                EFFECT_STATUS_PENDING,
                encode_utc_instant(available_at),
                claimed_run_version,
                now_text,
                now_text,
            ),
        )

    def insert_preflight_effect(
        self,
        conn: sqlite3.Connection,
        *,
        dispatch_id: str,
        source_event_id: str,
        run_id: str,
        available_at: datetime,
        claimed_run_version: int,
        now: datetime,
    ) -> None:
        from ai_dev_loop.scheduler.domain.cursor_contract import (
            PREFLIGHT_EFFECT_ID,
            PREFLIGHT_EFFECT_KIND,
        )

        self.insert_effect(
            conn,
            dispatch_id=dispatch_id,
            source_event_id=source_event_id,
            run_id=run_id,
            effect_id=PREFLIGHT_EFFECT_ID,
            effect_kind=PREFLIGHT_EFFECT_KIND,
            effect_payload={"effect_kind": PREFLIGHT_EFFECT_KIND},
            available_at=available_at,
            claimed_run_version=claimed_run_version,
            now=now,
        )

    def complete_effect_by_id(
        self,
        conn: sqlite3.Connection,
        *,
        dispatch_id: str,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_effects
            SET status = ?, completed_at = ?, updated_at = ?
            WHERE dispatch_id = ? AND status IN (?, ?)
            """,
            (
                EFFECT_STATUS_SUCCEEDED,
                now_text,
                now_text,
                dispatch_id,
                EFFECT_STATUS_PENDING,
                EFFECT_STATUS_CLAIMED,
            ),
        )
        return cursor.rowcount == 1

    def get_effect_by_dispatch_id(
        self, conn: sqlite3.Connection, dispatch_id: str
    ) -> sqlite3.Row | None:
        row = conn.execute(
            "SELECT * FROM scheduler_effects WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def get_latest_completed_cursor_attempt(
        self, conn: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT scheduler_attempts.*
            FROM scheduler_attempts
            JOIN scheduler_effects
              ON scheduler_effects.dispatch_id = scheduler_attempts.dispatch_id
            WHERE scheduler_attempts.run_id = ?
              AND scheduler_attempts.status IN (?, ?)
              AND scheduler_attempts.component = 'cursor'
              AND scheduler_attempts.ingested = 0
              AND scheduler_effects.effect_kind IN (?, ?)
            ORDER BY scheduler_attempts.completed_at DESC, scheduler_attempts.attempt_id DESC
            LIMIT 1
            """,
            (
                run_id,
                ATTEMPT_STATUS_COMPLETED,
                ATTEMPT_STATUS_FAILED,
                "cursor.create_chat",
                "cursor.run_turn",
            ),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def get_latest_completed_codex_attempt(
        self, conn: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row | None:
        from ai_dev_loop.scheduler.domain.codex_contract import CODEX_ATTEMPT_EFFECT_KINDS

        placeholders = ",".join("?" * len(CODEX_ATTEMPT_EFFECT_KINDS))
        row = conn.execute(
            f"""
            SELECT scheduler_attempts.*
            FROM scheduler_attempts
            JOIN scheduler_effects
              ON scheduler_effects.dispatch_id = scheduler_attempts.dispatch_id
            WHERE scheduler_attempts.run_id = ?
              AND scheduler_attempts.status IN (?, ?)
              AND scheduler_attempts.component = 'codex'
              AND scheduler_attempts.ingested = 0
              AND scheduler_effects.effect_kind IN ({placeholders})
            ORDER BY scheduler_attempts.completed_at DESC, scheduler_attempts.attempt_id DESC
            LIMIT 1
            """,
            (
                run_id,
                ATTEMPT_STATUS_COMPLETED,
                ATTEMPT_STATUS_FAILED,
                *CODEX_ATTEMPT_EFFECT_KINDS,
            ),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def get_latest_recorded_codex_attempt(
        self, conn: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row | None:
        from ai_dev_loop.scheduler.domain.codex_contract import CODEX_ATTEMPT_EFFECT_KINDS

        placeholders = ",".join("?" * len(CODEX_ATTEMPT_EFFECT_KINDS))
        row = conn.execute(
            f"""
            SELECT scheduler_attempts.*
            FROM scheduler_attempts
            JOIN scheduler_effects
              ON scheduler_effects.dispatch_id = scheduler_attempts.dispatch_id
            WHERE scheduler_attempts.run_id = ?
              AND scheduler_attempts.status IN (?, ?)
              AND scheduler_attempts.component = 'codex'
              AND scheduler_effects.effect_kind IN ({placeholders})
            ORDER BY scheduler_attempts.completed_at DESC, scheduler_attempts.attempt_id DESC
            LIMIT 1
            """,
            (
                run_id,
                ATTEMPT_STATUS_COMPLETED,
                ATTEMPT_STATUS_FAILED,
                *CODEX_ATTEMPT_EFFECT_KINDS,
            ),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def get_codex_bootstrap_attempt(
        self, conn: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row | None:
        from ai_dev_loop.scheduler.domain.codex_contract import BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND

        row = conn.execute(
            """
            SELECT scheduler_attempts.*
            FROM scheduler_attempts
            JOIN scheduler_effects
              ON scheduler_effects.dispatch_id = scheduler_attempts.dispatch_id
            WHERE scheduler_attempts.run_id = ?
              AND scheduler_attempts.status IN (?, ?)
              AND scheduler_attempts.component = 'codex'
              AND scheduler_effects.effect_kind = ?
            ORDER BY scheduler_attempts.completed_at ASC, scheduler_attempts.attempt_id ASC
            LIMIT 1
            """,
            (
                run_id,
                ATTEMPT_STATUS_COMPLETED,
                ATTEMPT_STATUS_FAILED,
                BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
            ),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def mark_attempt_ingested(
        self,
        conn: sqlite3.Connection,
        *,
        attempt_id: str,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_attempts
            SET ingested = 1, updated_at = ?
            WHERE attempt_id = ?
            """,
            (now_text, attempt_id),
        )
        return cursor.rowcount == 1

    def cancel_live_work(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        now: datetime,
    ) -> None:
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            UPDATE scheduler_effects
            SET status = ?, updated_at = ?,
                claim_id = NULL, claim_owner_id = NULL,
                claim_lease_generation = NULL, claimed_at = NULL
            WHERE run_id = ? AND status IN (?, ?, ?)
            """,
            (
                EFFECT_STATUS_CANCELLED,
                now_text,
                run_id,
                EFFECT_STATUS_PENDING,
                EFFECT_STATUS_CLAIMED,
                "retry_wait",
            ),
        )
        conn.execute(
            """
            UPDATE scheduler_timers
            SET status = ?, updated_at = ?
            WHERE run_id = ? AND status = ?
            """,
            (TIMER_STATUS_CANCELLED, now_text, run_id, TIMER_STATUS_PENDING),
        )
        conn.execute(
            """
            UPDATE scheduler_claims
            SET status = 'stale', released_at = ?, updated_at = ?
            WHERE run_id = ? AND status = 'active'
            """,
            (now_text, now_text, run_id),
        )
        conn.execute(
            """
            UPDATE scheduler_run_tick_claims
            SET status = 'stale', released_at = ?, updated_at = ?
            WHERE run_id = ? AND status = 'active'
            """,
            (now_text, now_text, run_id),
        )

    def cancel_nonterminal_attempts(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        completion_fence_id: str,
        now: datetime,
    ) -> list[sqlite3.Row]:
        now_text = encode_utc_instant(now)
        rows = conn.execute(
            """
            SELECT * FROM scheduler_attempts
            WHERE run_id = ? AND status IN (?, ?, ?)
            """,
            (
                run_id,
                ATTEMPT_STATUS_LAUNCHING,
                ATTEMPT_STATUS_ACTIVE,
                ATTEMPT_STATUS_UNCERTAIN,
            ),
        ).fetchall()
        conn.execute(
            """
            UPDATE scheduler_attempts
            SET status = ?, completion_fence_id = ?, updated_at = ?
            WHERE run_id = ? AND status IN (?, ?, ?)
              AND completion_fence_id IS NULL
            """,
            (
                ATTEMPT_STATUS_CANCELLED,
                completion_fence_id,
                now_text,
                run_id,
                ATTEMPT_STATUS_LAUNCHING,
                ATTEMPT_STATUS_ACTIVE,
                ATTEMPT_STATUS_UNCERTAIN,
            ),
        )
        return [cast(sqlite3.Row, row) for row in rows]

    def release_capacity_for_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_capacity
            SET holder_run_id = NULL, holder_claim_id = NULL,
                holder_tick_generation = NULL, updated_at = ?
            WHERE capacity_name = ? AND holder_run_id = ?
            """,
            (now_text, CAPACITY_NAME, run_id),
        )
        return cursor.rowcount == 1

    def list_events_for_run(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        *,
        limit: int,
        newest_first: bool,
    ) -> list[sqlite3.Row]:
        if limit < 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "history limit must be positive",
            )
        order = "DESC" if newest_first else "ASC"
        rows = conn.execute(
            f"""
            SELECT event_id, run_id, sequence, event_kind,
                   event_payload, event_payload_sha256, created_at
            FROM scheduler_events
            WHERE run_id = ?
            ORDER BY sequence {order}
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        return [cast(sqlite3.Row, row) for row in rows]

    def get_nonterminal_attempt_rows(
        self, conn: sqlite3.Connection, run_id: str
    ) -> list[sqlite3.Row]:
        placeholders = ",".join("?" * len(NON_TERMINAL_ATTEMPT_STATUSES))
        rows = conn.execute(
            f"""
            SELECT * FROM scheduler_attempts
            WHERE run_id = ? AND status IN ({placeholders})
            ORDER BY created_at ASC, attempt_id ASC
            """,
            (run_id, *NON_TERMINAL_ATTEMPT_STATUSES),
        ).fetchall()
        return [cast(sqlite3.Row, row) for row in rows]

    def list_attempt_timeline_rows(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        *,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[sqlite3.Row]:
        order = "DESC" if newest_first else "ASC"
        query = f"""
            SELECT attempt_id, iteration, component, status,
                   launch_requested_at, completed_at, phase_attempt
            FROM (
                SELECT attempt_id, iteration, component, status,
                       launch_requested_at, completed_at, created_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY iteration, component
                           ORDER BY
                               COALESCE(launch_requested_at, created_at) ASC,
                               created_at ASC,
                               attempt_id ASC
                       ) AS phase_attempt
                FROM scheduler_attempts
                WHERE run_id = ?
            )
            ORDER BY
                COALESCE(launch_requested_at, created_at) {order},
                created_at {order},
                attempt_id {order}
        """
        params: list[object] = [run_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(query, tuple(params)).fetchall()
        return [cast(sqlite3.Row, row) for row in rows]

    @staticmethod
    def dump_sequence_state(state: object) -> tuple[str, str, str]:
        from ai_dev_loop.scheduler.domain.sequence import (
            ABORT_PENDING_SEQUENCE_STATE_ADAPTER,
            ABORT_PENDING_SEQUENCE_STATE_KIND,
            ABORTED_SEQUENCE_STATE_ADAPTER,
            ABORTED_SEQUENCE_STATE_KIND,
            ACTIVE_SEQUENCE_STATE_ADAPTER,
            ACTIVE_SEQUENCE_STATE_KIND,
            AWAITING_FINALIZATION_SEQUENCE_STATE_ADAPTER,
            AWAITING_FINALIZATION_SEQUENCE_STATE_KIND,
            BLOCKED_SEQUENCE_STATE_ADAPTER,
            BLOCKED_SEQUENCE_STATE_KIND,
            PREPARED_SEQUENCE_STATE_ADAPTER,
            PREPARED_SEQUENCE_STATE_KIND,
            AbortedSequenceState,
            AbortPendingSequenceState,
            ActiveSequenceState,
            AwaitingFinalizationSequenceState,
            BlockedSequenceState,
            PreparedSequenceState,
        )

        if isinstance(state, PreparedSequenceState):
            validated = PREPARED_SEQUENCE_STATE_ADAPTER.validate_python(
                state.model_dump(mode="json")
            )
            text = PREPARED_SEQUENCE_STATE_ADAPTER.dump_json(validated).decode("utf-8")
            return PREPARED_SEQUENCE_STATE_KIND, text, payload_sha256(text)
        if isinstance(state, ActiveSequenceState):
            active_validated = ACTIVE_SEQUENCE_STATE_ADAPTER.validate_python(
                state.model_dump(mode="json")
            )
            text = ACTIVE_SEQUENCE_STATE_ADAPTER.dump_json(active_validated).decode("utf-8")
            return ACTIVE_SEQUENCE_STATE_KIND, text, payload_sha256(text)
        if isinstance(state, AbortPendingSequenceState):
            pending_validated = ABORT_PENDING_SEQUENCE_STATE_ADAPTER.validate_python(
                state.model_dump(mode="json")
            )
            text = ABORT_PENDING_SEQUENCE_STATE_ADAPTER.dump_json(pending_validated).decode("utf-8")
            return ABORT_PENDING_SEQUENCE_STATE_KIND, text, payload_sha256(text)
        if isinstance(state, BlockedSequenceState):
            blocked_validated = BLOCKED_SEQUENCE_STATE_ADAPTER.validate_python(
                state.model_dump(mode="json")
            )
            text = BLOCKED_SEQUENCE_STATE_ADAPTER.dump_json(blocked_validated).decode("utf-8")
            return BLOCKED_SEQUENCE_STATE_KIND, text, payload_sha256(text)
        if isinstance(state, AbortedSequenceState):
            aborted_validated = ABORTED_SEQUENCE_STATE_ADAPTER.validate_python(
                state.model_dump(mode="json")
            )
            text = ABORTED_SEQUENCE_STATE_ADAPTER.dump_json(aborted_validated).decode("utf-8")
            return ABORTED_SEQUENCE_STATE_KIND, text, payload_sha256(text)
        if isinstance(state, AwaitingFinalizationSequenceState):
            finalized_validated = AWAITING_FINALIZATION_SEQUENCE_STATE_ADAPTER.validate_python(
                state.model_dump(mode="json")
            )
            text = AWAITING_FINALIZATION_SEQUENCE_STATE_ADAPTER.dump_json(
                finalized_validated
            ).decode("utf-8")
            return AWAITING_FINALIZATION_SEQUENCE_STATE_KIND, text, payload_sha256(text)
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.INTERNAL,
            "sequence state has unexpected type",
        )

    @staticmethod
    def dump_sequence_entry(entry: object) -> tuple[str, str]:
        from ai_dev_loop.scheduler.domain.sequence import (
            FROZEN_SEQUENCE_ENTRY_ADAPTER,
            FrozenSequenceEntry,
        )

        if not isinstance(entry, FrozenSequenceEntry):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.INTERNAL,
                "sequence entry has unexpected type",
            )
        validated = FROZEN_SEQUENCE_ENTRY_ADAPTER.validate_python(entry.model_dump(mode="json"))
        text = FROZEN_SEQUENCE_ENTRY_ADAPTER.dump_json(validated).decode("utf-8")
        return text, payload_sha256(text)

    @staticmethod
    def load_sequence_state(payload: str) -> SequencePersistedState:
        from ai_dev_loop.scheduler.domain.sequence import (
            ABORT_PENDING_SEQUENCE_STATE_ADAPTER,
            ABORTED_SEQUENCE_STATE_ADAPTER,
            ACTIVE_SEQUENCE_STATE_ADAPTER,
            AWAITING_FINALIZATION_SEQUENCE_STATE_ADAPTER,
            BLOCKED_SEQUENCE_STATE_ADAPTER,
            PREPARED_SEQUENCE_STATE_ADAPTER,
            AbortedSequenceState,
            AbortPendingSequenceState,
            ActiveSequenceState,
            AwaitingFinalizationSequenceState,
            BlockedSequenceState,
            PreparedSequenceState,
        )

        for adapter in (
            PREPARED_SEQUENCE_STATE_ADAPTER,
            ACTIVE_SEQUENCE_STATE_ADAPTER,
            ABORT_PENDING_SEQUENCE_STATE_ADAPTER,
            BLOCKED_SEQUENCE_STATE_ADAPTER,
            ABORTED_SEQUENCE_STATE_ADAPTER,
            AWAITING_FINALIZATION_SEQUENCE_STATE_ADAPTER,
        ):
            try:
                loaded = adapter.validate_json(payload)
            except Exception:
                continue
            if isinstance(
                loaded,
                (
                    PreparedSequenceState,
                    ActiveSequenceState,
                    AbortPendingSequenceState,
                    BlockedSequenceState,
                    AbortedSequenceState,
                    AwaitingFinalizationSequenceState,
                ),
            ):
                return loaded
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence payload has unexpected type",
        )

    def get_sequence_by_idempotency_key(
        self,
        conn: sqlite3.Connection,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        row = conn.execute(
            "SELECT * FROM scheduler_sequences WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def find_existing_prepared_sequence(
        self,
        conn: sqlite3.Connection,
        *,
        definition: object,
        resubmission_id: str | None,
    ) -> sqlite3.Row | None:
        from ai_dev_loop.scheduler.application.sequence_prepare import _sequence_idempotency_key
        from ai_dev_loop.scheduler.domain.sequence import (
            PreparedSequenceDefinition,
            sequence_identity_payload,
        )

        if not isinstance(definition, PreparedSequenceDefinition):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.INTERNAL,
                "sequence definition has unexpected type",
            )
        key = _sequence_idempotency_key(definition, resubmission_id=resubmission_id)
        existing = self.get_sequence_by_idempotency_key(conn, key)
        if existing is not None:
            return existing
        return self.find_sequence_by_matching_identity(
            conn,
            worktree_key=definition.repository.worktree_key,
            identity=sequence_identity_payload(definition),
            resubmission_id=resubmission_id,
        )

    def find_sequence_by_matching_identity(
        self,
        conn: sqlite3.Connection,
        *,
        worktree_key: str,
        identity: dict[str, object],
        resubmission_id: str | None,
    ) -> sqlite3.Row | None:
        from ai_dev_loop.scheduler.application.sequence_prepare import _sequence_idempotency_key
        from ai_dev_loop.scheduler.domain.sequence import (
            sequence_identity_payload,
        )

        rows = conn.execute(
            """
            SELECT sequence_id FROM scheduler_sequences
            WHERE worktree_key = ?
            ORDER BY prepared_at ASC, sequence_id ASC
            """,
            (worktree_key,),
        ).fetchall()
        for row in rows:
            sequence_id = str(row[0])
            state = self.load_validated_sequence_state(conn, sequence_id)
            if sequence_identity_payload(state.definition) != identity:
                continue
            stored_key = _sequence_idempotency_key(
                state.definition,
                resubmission_id=resubmission_id,
            )
            if state.idempotency_key == stored_key:
                existing = self.get_sequence_by_idempotency_key(conn, state.idempotency_key)
                if existing is not None:
                    return existing
        return None

    def get_sequence_row(self, conn: sqlite3.Connection, sequence_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM scheduler_sequences WHERE sequence_id = ?",
            (sequence_id,),
        ).fetchone()
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.NOT_FOUND,
                f"prepared sequence not found: {sequence_id}",
            )
        return cast(sqlite3.Row, row)

    def load_sequence_state_only(
        self,
        conn: sqlite3.Connection,
        sequence_id: str,
    ) -> SequencePersistedState:
        row = self.get_sequence_row(conn, sequence_id)
        state = self.load_sequence_state(row["payload"])
        if state.sequence_id != sequence_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence_id disagrees with primary key",
            )
        return state

    def load_validated_sequence_state(
        self,
        conn: sqlite3.Connection,
        sequence_id: str,
        *,
        validate_lineage: bool = True,
    ) -> SequencePersistedState:
        from ai_dev_loop.scheduler.domain.common import worktree_key
        from ai_dev_loop.scheduler.domain.sequence import (
            ABORT_PENDING_SEQUENCE_STATE_KIND,
            ABORTED_SEQUENCE_STATE_KIND,
            ACTIVE_SEQUENCE_STATE_KIND,
            AWAITING_FINALIZATION_SEQUENCE_STATE_KIND,
            BLOCKED_SEQUENCE_STATE_KIND,
            FROZEN_SEQUENCE_ENTRY_ADAPTER,
            PREPARED_SEQUENCE_STATE_KIND,
            AbortedSequenceState,
            AbortPendingSequenceState,
            ActiveSequenceState,
            AwaitingFinalizationSequenceState,
            BlockedSequenceState,
            PreparedSequenceState,
        )

        row = self.get_sequence_row(conn, sequence_id)
        state = self.load_sequence_state(row["payload"])
        row_kind = str(row["state_kind"])
        kind_to_type = {
            PREPARED_SEQUENCE_STATE_KIND: PreparedSequenceState,
            ACTIVE_SEQUENCE_STATE_KIND: ActiveSequenceState,
            ABORT_PENDING_SEQUENCE_STATE_KIND: AbortPendingSequenceState,
            BLOCKED_SEQUENCE_STATE_KIND: BlockedSequenceState,
            ABORTED_SEQUENCE_STATE_KIND: AbortedSequenceState,
            AWAITING_FINALIZATION_SEQUENCE_STATE_KIND: AwaitingFinalizationSequenceState,
        }
        if row_kind not in kind_to_type:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence state_kind is unsupported",
            )
        if not isinstance(state, kind_to_type[row_kind]):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence state_kind mismatch",
            )
        if payload_sha256(row["payload"]) != row["payload_sha256"]:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence payload hash mismatch",
            )
        if state.sequence_id != sequence_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence_id disagrees with primary key",
            )
        if state.definition.sequence_id != sequence_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "definition.sequence_id disagrees with primary key",
            )
        if int(row["version"]) != state.version:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence version mismatch",
            )
        if str(row["name"]) != state.definition.name:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence name mismatch",
            )
        if str(row["project_name"]) != state.definition.project_name:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence project_name mismatch",
            )
        if str(row["idempotency_key"]) != state.idempotency_key:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence idempotency_key mismatch",
            )
        if int(row["entry_count"]) != len(state.definition.entries):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence entry_count mismatch",
            )
        payload_root = str(state.definition.repository.root)
        if str(row["repository_root"]) != payload_root:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "repository root disagrees with indexed repository_root",
            )
        if worktree_key(payload_root) != str(row["worktree_key"]):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "repository identity disagrees with indexed worktree key",
            )
        entry_rows = conn.execute(
            """
            SELECT ordinal, phase_name, planned_run_id, payload, payload_sha256
            FROM scheduler_sequence_entries
            WHERE sequence_id = ?
            ORDER BY ordinal ASC
            """,
            (sequence_id,),
        ).fetchall()
        if len(entry_rows) != len(state.definition.entries):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence entry count mismatch",
            )
        for index, entry_row in enumerate(entry_rows):
            entry = state.definition.entries[index]
            if int(entry_row["ordinal"]) != entry.ordinal:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CORRUPTION,
                    "sequence entry ordinal mismatch",
                )
            if str(entry_row["phase_name"]) != entry.phase_name:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CORRUPTION,
                    "sequence entry phase_name mismatch",
                )
            if str(entry_row["planned_run_id"]) != entry.planned_run_id:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CORRUPTION,
                    "sequence entry planned_run_id mismatch",
                )
            entry_payload = str(entry_row["payload"])
            if payload_sha256(entry_payload) != str(entry_row["payload_sha256"]):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CORRUPTION,
                    "sequence entry payload hash mismatch",
                )
            try:
                parsed_entry = FROZEN_SEQUENCE_ENTRY_ADAPTER.validate_json(entry_payload)
            except Exception as exc:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CORRUPTION,
                    "sequence entry payload failed validation",
                ) from exc
            if parsed_entry != entry:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CORRUPTION,
                    "sequence entry payload disagrees with aggregate state",
                )
        from ai_dev_loop.scheduler.application.sequence_materializer import frozen_entry_hash
        from ai_dev_loop.scheduler.domain.sequence_lifecycle_validation import (
            SequenceLifecycleValidationError,
            validate_sequence_lifecycle_state,
        )

        materialized_entries = getattr(state, "materialized_entries", None)
        if materialized_entries:
            for materialized in materialized_entries:
                entry = state.definition.entries[materialized.ordinal - 1]
                if frozen_entry_hash(entry) != materialized.entry_hash:
                    raise SchedulerEngineError(
                        SchedulerEngineErrorKind.CORRUPTION,
                        "materialized entry_hash disagrees with frozen entry payload",
                    )
                if (
                    not self.schema_supports_sequence_run_lineage(conn)
                    and materialized.run_id != entry.planned_run_id
                ):
                    raise SchedulerEngineError(
                        SchedulerEngineErrorKind.CORRUPTION,
                        "materialized run_id disagrees with planned run binding",
                    )
        if isinstance(
            state,
            (AbortPendingSequenceState, BlockedSequenceState, AbortedSequenceState),
        ):
            try:
                validate_sequence_lifecycle_state(state)
            except SequenceLifecycleValidationError as exc:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CORRUPTION,
                    str(exc),
                ) from exc
        if validate_lineage and self.schema_supports_sequence_run_lineage(conn):
            from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
                load_and_validate_sequence_lineage,
            )

            load_and_validate_sequence_lineage(conn, state)
        return state

    def schema_supports_sequence_run_lineage(self, conn: sqlite3.Connection) -> bool:
        return self._user_version(conn) >= SEQUENCE_RUN_LINEAGE_SCHEMA_VERSION

    def schema_supports_sequence_review_recovery(self, conn: sqlite3.Connection) -> bool:
        return self._user_version(conn) >= SEQUENCE_REVIEW_RECOVERY_SCHEMA_VERSION

    def insert_prepared_sequence(
        self,
        conn: sqlite3.Connection,
        *,
        state: PreparedSequenceState,
        now: datetime,
    ) -> None:
        from ai_dev_loop.scheduler.domain.sequence import PreparedSequenceState

        if not isinstance(state, PreparedSequenceState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "insert accepts only PreparedSequenceState",
            )
        kind, payload, digest = self.dump_sequence_state(state)
        definition = state.definition
        now_text = encode_utc_instant(now)
        try:
            conn.execute(
                """
                INSERT INTO scheduler_sequences(
                    sequence_id, name, state_kind, project_name, repository_root,
                    worktree_key, entry_count, idempotency_key, version,
                    prepared_at, updated_at, payload, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.sequence_id,
                    definition.name,
                    kind,
                    definition.project_name,
                    definition.repository.root,
                    definition.repository.worktree_key,
                    len(definition.entries),
                    state.idempotency_key,
                    state.version,
                    state.prepared_at,
                    now_text,
                    payload,
                    digest,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "prepared sequence identity already exists",
            ) from exc
        for entry in definition.entries:
            entry_payload, entry_digest = self.dump_sequence_entry(entry)
            conn.execute(
                """
                INSERT INTO scheduler_sequence_entries(
                    sequence_id, ordinal, phase_name, planned_run_id,
                    payload, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    state.sequence_id,
                    entry.ordinal,
                    entry.phase_name,
                    entry.planned_run_id,
                    entry_payload,
                    entry_digest,
                ),
            )

    def compare_and_swap_sequence_state(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_id: str,
        expected_version: int,
        new_state: SequencePersistedState,
        now: datetime,
        validate_lineage: bool = True,
    ) -> bool:
        from ai_dev_loop.scheduler.domain.sequence import (
            AbortedSequenceState,
            AbortPendingSequenceState,
            ActiveSequenceState,
            AwaitingFinalizationSequenceState,
            BlockedSequenceState,
            PreparedSequenceState,
        )

        if not isinstance(
            new_state,
            (
                PreparedSequenceState,
                ActiveSequenceState,
                AbortPendingSequenceState,
                BlockedSequenceState,
                AbortedSequenceState,
                AwaitingFinalizationSequenceState,
            ),
        ):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "compare_and_swap received unsupported sequence state type",
            )
        if new_state.sequence_id != sequence_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "replacement sequence_id disagrees with target",
            )
        current_row = self.get_sequence_row(conn, sequence_id)
        current_version = int(current_row["version"])
        if current_version != expected_version:
            return False
        if new_state.version != expected_version + 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "replacement version must equal expected_version + 1",
            )
        current_state = self.load_validated_sequence_state(
            conn,
            sequence_id,
            validate_lineage=validate_lineage,
        )
        if new_state.prepared_at != current_state.prepared_at:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "prepared_at is immutable",
            )
        if new_state.idempotency_key != current_state.idempotency_key:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "idempotency_key is immutable",
            )
        if new_state.definition != current_state.definition:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence definition is immutable",
            )
        from ai_dev_loop.scheduler.domain.sequence_lifecycle_validation import (
            SequenceLifecycleValidationError,
            validate_sequence_lifecycle_transition,
        )

        try:
            validate_sequence_lifecycle_transition(current_state, new_state)
        except SequenceLifecycleValidationError as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                str(exc),
            ) from exc
        kind, payload, digest = self.dump_sequence_state(new_state)
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_sequences
            SET state_kind = ?, payload = ?, payload_sha256 = ?,
                version = ?, updated_at = ?
            WHERE sequence_id = ? AND version = ?
            """,
            (
                kind,
                payload,
                digest,
                new_state.version,
                now_text,
                sequence_id,
                expected_version,
            ),
        )
        return cursor.rowcount == 1

    def get_review_retry_generation_row(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        failure_generation: int,
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT * FROM scheduler_review_retry_generations
            WHERE run_id = ? AND failure_generation = ?
            """,
            (run_id, failure_generation),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def insert_review_retry_generation(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        failure_generation: int,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            INSERT INTO scheduler_review_retry_generations(
                run_id, failure_generation, scheduled_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(run_id, failure_generation) DO NOTHING
            """,
            (run_id, failure_generation, now_text),
        )
        return cursor.rowcount == 1

    def get_capacity_retry_generation_row(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        capacity_wait_generation: int,
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT * FROM scheduler_capacity_retry_generations
            WHERE run_id = ? AND capacity_wait_generation = ?
            """,
            (run_id, capacity_wait_generation),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def insert_capacity_retry_generation(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        capacity_wait_generation: int,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            INSERT INTO scheduler_capacity_retry_generations(
                run_id, capacity_wait_generation, scheduled_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(run_id, capacity_wait_generation) DO NOTHING
            """,
            (run_id, capacity_wait_generation, now_text),
        )
        return cursor.rowcount == 1

    def get_latest_review_recovery_successor(
        self,
        conn: sqlite3.Connection,
        *,
        source_run_id: str,
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT * FROM scheduler_review_recovery_successors
            WHERE source_run_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (source_run_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def get_cursor_turn_attempt_for_iteration(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        iteration: int,
    ) -> sqlite3.Row | None:
        rows = conn.execute(
            """
            SELECT scheduler_attempts.*, scheduler_effects.effect_payload
            FROM scheduler_attempts
            JOIN scheduler_effects
              ON scheduler_effects.dispatch_id = scheduler_attempts.dispatch_id
            WHERE scheduler_attempts.run_id = ?
              AND scheduler_attempts.component = 'cursor'
              AND scheduler_attempts.status IN (?, ?)
              AND scheduler_effects.effect_kind = ?
            ORDER BY scheduler_attempts.completed_at DESC, scheduler_attempts.attempt_id DESC
            """,
            (
                run_id,
                ATTEMPT_STATUS_COMPLETED,
                ATTEMPT_STATUS_FAILED,
                "cursor.run_turn",
            ),
        ).fetchall()
        for row in rows:
            payload = row["effect_payload"]
            if isinstance(payload, str):
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    continue
            elif isinstance(payload, dict):
                parsed = payload
            else:
                continue
            if int(parsed.get("iteration", 0)) == iteration:
                return cast(sqlite3.Row, row)
        return None

    def get_review_recovery_source_for_successor(
        self,
        conn: sqlite3.Connection,
        *,
        successor_run_id: str,
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT * FROM scheduler_review_recovery_successors
            WHERE successor_run_id = ?
            LIMIT 1
            """,
            (successor_run_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def get_review_recovery_successor(
        self,
        conn: sqlite3.Connection,
        *,
        source_run_id: str,
        recovery_key: str,
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT * FROM scheduler_review_recovery_successors
            WHERE source_run_id = ? AND recovery_key = ?
            """,
            (source_run_id, recovery_key),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def insert_review_recovery_successor(
        self,
        conn: sqlite3.Connection,
        *,
        source_run_id: str,
        recovery_key: str,
        successor_run_id: str,
        now: datetime,
    ) -> bool:
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            INSERT INTO scheduler_review_recovery_successors(
                source_run_id, recovery_key, successor_run_id, created_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(source_run_id, recovery_key) DO NOTHING
            """,
            (source_run_id, recovery_key, successor_run_id, now_text),
        )
        return cursor.rowcount == 1

    def list_sequence_execution_replacement_intents_for_source(
        self,
        conn: sqlite3.Connection,
        *,
        source_run_id: str,
    ) -> list[sqlite3.Row]:
        rows = conn.execute(
            """
            SELECT *
            FROM scheduler_sequence_execution_replacements
            WHERE source_run_id = ?
            ORDER BY created_at ASC
            """,
            (source_run_id,),
        ).fetchall()
        return [cast(sqlite3.Row, row) for row in rows]

    def get_sequence_execution_replacement_intent_by_successor(
        self,
        conn: sqlite3.Connection,
        *,
        successor_run_id: str,
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT *
            FROM scheduler_sequence_execution_replacements
            WHERE successor_run_id = ?
            LIMIT 1
            """,
            (successor_run_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def get_sequence_execution_replacement_intent(
        self,
        conn: sqlite3.Connection,
        *,
        source_run_id: str,
        recovery_key: str,
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT *
            FROM scheduler_sequence_execution_replacements
            WHERE source_run_id = ? AND recovery_key = ?
            """,
            (source_run_id, recovery_key),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def insert_sequence_execution_replacement_intent(
        self,
        conn: sqlite3.Connection,
        *,
        intent: object,
        intent_payload: str,
        intent_digest: str,
        now: datetime,
    ) -> bool:
        from ai_dev_loop.scheduler.domain.sequence_execution_replacement import (
            SequenceExecutionReplacementIntent,
        )

        if not isinstance(intent, SequenceExecutionReplacementIntent):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "insert_sequence_execution_replacement_intent requires intent model",
            )
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            INSERT INTO scheduler_sequence_execution_replacements(
                sequence_id, ordinal, source_run_id, source_generation, recovery_key,
                successor_run_id, successor_generation, intent_payload,
                intent_payload_sha256, created_at, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(source_run_id, recovery_key) DO NOTHING
            """,
            (
                intent.sequence_id,
                intent.ordinal,
                intent.source_run_id,
                intent.source_generation,
                intent.recovery_key,
                intent.successor_run_id,
                intent.successor_generation,
                intent_payload,
                intent_digest,
                now_text,
            ),
        )
        return cursor.rowcount == 1

    def mark_sequence_execution_replacement_published(
        self,
        conn: sqlite3.Connection,
        *,
        source_run_id: str,
        recovery_key: str,
        now: datetime,
    ) -> None:
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            UPDATE scheduler_sequence_execution_replacements
            SET published_at = ?
            WHERE source_run_id = ? AND recovery_key = ? AND published_at IS NULL
            """,
            (now_text, source_run_id, recovery_key),
        )

    def is_sequence_execution_replacement_cancelled(
        self,
        conn: sqlite3.Connection,
        *,
        source_run_id: str,
        recovery_key: str,
    ) -> bool:
        if not self.schema_supports_sequence_review_recovery(conn):
            return False
        row = self.get_sequence_execution_replacement_intent(
            conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
        )
        if row is None:
            return False
        return row["cancelled_at"] is not None

    def cancel_outstanding_sequence_execution_replacements(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_id: str,
        source_run_id: str,
        now: datetime,
    ) -> int:
        if not self.schema_supports_sequence_review_recovery(conn):
            return 0
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_sequence_execution_replacements
            SET cancelled_at = ?
            WHERE sequence_id = ?
              AND source_run_id = ?
              AND published_at IS NULL
              AND cancelled_at IS NULL
            """,
            (now_text, sequence_id, source_run_id),
        )
        return int(cursor.rowcount)

    def insert_sequence_recovery_pending_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        state: SchedulerState,
        event_id: str,
        event: SchedulerEvent,
        now: datetime,
    ) -> None:
        from ai_dev_loop.scheduler.domain.state import PendingSequenceReviewRecoveryState

        kind, payload, digest = self.dump_state(state)
        if kind != "pending_sequence_review_recovery":
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence recovery pending insert requires pending_sequence_review_recovery",
            )
        if not isinstance(state, PendingSequenceReviewRecoveryState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence recovery pending insert received unexpected state type",
            )
        now_text = encode_utc_instant(now)
        worktree_key = state.context.repository.worktree_key
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                kind,
                payload,
                digest,
                state.version,
                state.idempotency_key,
                worktree_key,
                now_text,
                now_text,
            ),
        )
        event_kind, event_payload, event_digest = self.dump_event(event)
        conn.execute(
            """
            INSERT INTO scheduler_events(
                event_id, run_id, sequence, event_kind,
                event_payload, event_payload_sha256, created_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (event_id, run_id, event_kind, event_payload, event_digest, now_text),
        )

    def activate_sequence_recovery_successor_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        pending_state: SchedulerState,
        active_state: SchedulerState,
        now: datetime,
    ) -> None:
        from ai_dev_loop.scheduler.domain.state import (
            AwaitingCodexReviewState,
            PendingSequenceReviewRecoveryState,
        )

        if not isinstance(pending_state, PendingSequenceReviewRecoveryState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "activate_sequence_recovery_successor_run requires pending state",
            )
        if not isinstance(active_state, AwaitingCodexReviewState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "activate_sequence_recovery_successor_run requires awaiting_codex_review",
            )
        if pending_state.run_id != run_id or active_state.run_id != run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "activate_sequence_recovery_successor_run run_id mismatch",
            )
        kind, payload, digest = self.dump_state(active_state)
        now_text = encode_utc_instant(now)
        cursor = conn.execute(
            """
            UPDATE scheduler_runs
            SET state_kind = ?, state_payload = ?, state_payload_sha256 = ?,
                version = ?, updated_at = ?
            WHERE run_id = ? AND state_kind = ? AND version = ?
            """,
            (
                kind,
                payload,
                digest,
                active_state.version,
                now_text,
                run_id,
                "pending_sequence_review_recovery",
                pending_state.version,
            ),
        )
        if cursor.rowcount != 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence recovery successor activation lost a concurrent update",
            )
        worktree_key = active_state.context.repository.worktree_key
        reservation = conn.execute(
            """
            INSERT INTO scheduler_repository_reservations(
                worktree_key, run_id, repository_root, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(worktree_key) DO UPDATE SET
                run_id = excluded.run_id,
                repository_root = excluded.repository_root,
                status = excluded.status,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            WHERE scheduler_repository_reservations.status = 'released'
            """,
            (
                worktree_key,
                run_id,
                active_state.context.repository.root,
                ReservationStatus.ACTIVE.value,
                now_text,
                now_text,
            ),
        )
        if reservation.rowcount != 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "repository worktree reservation could not be claimed for recovery successor",
            )

    def insert_recovery_review_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        state: SchedulerState,
        event_id: str,
        event: SchedulerEvent,
        now: datetime,
    ) -> None:
        kind, payload, digest = self.dump_state(state)
        if kind != "awaiting_codex_review":
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery insert accepts only awaiting_codex_review state",
            )
        now_text = encode_utc_instant(now)
        worktree_key = state.context.repository.worktree_key
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                kind,
                payload,
                digest,
                state.version,
                state.idempotency_key,
                worktree_key,
                now_text,
                now_text,
            ),
        )
        event_kind, event_payload, event_digest = self.dump_event(event)
        conn.execute(
            """
            INSERT INTO scheduler_events(
                event_id, run_id, sequence, event_kind,
                event_payload, event_payload_sha256, created_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (event_id, run_id, event_kind, event_payload, event_digest, now_text),
        )
        reservation = conn.execute(
            """
            INSERT INTO scheduler_repository_reservations(
                worktree_key, run_id, repository_root, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(worktree_key) DO UPDATE SET
                run_id = excluded.run_id,
                repository_root = excluded.repository_root,
                status = excluded.status,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            WHERE scheduler_repository_reservations.status = 'released'
            """,
            (
                worktree_key,
                run_id,
                state.context.repository.root,
                ReservationStatus.ACTIVE.value,
                now_text,
                now_text,
            ),
        )
        if reservation.rowcount != 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "repository worktree reservation could not be claimed for recovery successor",
            )

    def load_sequence_run_lineage(self, conn: sqlite3.Connection, sequence_id: str) -> object:
        from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
            load_sequence_run_lineage,
        )

        return load_sequence_run_lineage(conn, sequence_id)

    def insert_sequence_run_attempt(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_id: str,
        ordinal: int,
        attempt: object,
        planned_run_id: str,
    ) -> None:
        from ai_dev_loop.scheduler.domain.sequence_run_lineage import SequenceRunAttempt
        from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
            insert_sequence_run_attempt,
        )

        if not isinstance(attempt, SequenceRunAttempt):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "insert_sequence_run_attempt requires SequenceRunAttempt",
            )
        insert_sequence_run_attempt(
            conn,
            sequence_id=sequence_id,
            ordinal=ordinal,
            attempt=attempt,
            planned_run_id=planned_run_id,
        )

    def resolve_sequence_attempt_terminal(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_id: str,
        ordinal: int,
        run_id: str,
        terminal_outcome: str,
        resolved_at: datetime,
    ) -> None:
        from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
            resolve_sequence_attempt_terminal,
        )

        resolve_sequence_attempt_terminal(
            conn,
            sequence_id=sequence_id,
            ordinal=ordinal,
            run_id=run_id,
            terminal_outcome=terminal_outcome,  # type: ignore[arg-type]
            resolved_at=resolved_at,
        )

    def compare_and_swap_sequence_execution_leaf(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_id: str,
        ordinal: int,
        expectation: object,
        expected_current_run_id: str,
        replacement_attempt: object,
        updated_sequence_state: object,
        now: datetime,
    ) -> bool:
        from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState
        from ai_dev_loop.scheduler.domain.sequence_run_lineage import (
            SequenceRunAttempt,
        )
        from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
            SequenceExecutionCASExpectation,
            compare_and_swap_sequence_execution_leaf,
        )

        if not isinstance(expectation, SequenceExecutionCASExpectation):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "compare_and_swap_sequence_execution_leaf requires expectation",
            )
        if not isinstance(replacement_attempt, SequenceRunAttempt):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "compare_and_swap_sequence_execution_leaf requires SequenceRunAttempt",
            )
        if not isinstance(updated_sequence_state, ActiveSequenceState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "compare_and_swap_sequence_execution_leaf requires active sequence state",
            )
        return compare_and_swap_sequence_execution_leaf(
            conn,
            self,
            sequence_id=sequence_id,
            ordinal=ordinal,
            expectation=expectation,
            expected_current_run_id=expected_current_run_id,
            replacement_attempt=replacement_attempt,
            updated_sequence_state=updated_sequence_state,
            now=now,
        )
