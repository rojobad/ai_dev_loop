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
from typing import cast

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
    SchedulerEvent,
    parse_scheduler_event,
)
from ai_dev_loop.scheduler.domain.state import (
    SCHEDULER_STATE_ADAPTER,
    SchedulerState,
    SubmittedState,
)

SCHEMA_VERSION = 4
MIGRATION_V1_NAME = "0001_initial"
MIGRATION_V2_NAME = "0002_tick_control"
MIGRATION_V3_NAME = "0003_attempt_executor"
MIGRATION_V4_NAME = "0004_cursor_workflow"
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
    }
)
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
    }
)
NON_TERMINAL_STATE_KINDS = frozenset(
    {
        "queued",
        "authorized",
        "admitted",
        "preflight_complete",
        "cursor_ready",
        "waiting_usage_limit",
        "awaiting_codex_review",
        "waiting_for_cursor_fix",
        "blocked",
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
        "awaiting_codex_review",
        "waiting_for_cursor_fix",
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
            store._verify_current_schema(conn)
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
            elif version == 1:
                self._verify_migration_checksum(conn, 1)
                self._migrate_v1_to_v2(conn)
                self._migrate_v2_to_v3(conn)
                self._migrate_v3_to_v4(conn)
            elif version == 2:
                self._verify_migration_checksum(conn, 1)
                self._verify_migration_checksum(conn, 2)
                self._migrate_v2_to_v3(conn)
                self._migrate_v3_to_v4(conn)
            elif version == 3:
                for migration_version in (1, 2, 3):
                    self._verify_migration_checksum(conn, migration_version)
                self._migrate_v3_to_v4(conn)
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

    def _verify_current_schema(self, conn: sqlite3.Connection) -> None:
        version = self._user_version(conn)
        if version != SCHEMA_VERSION:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                f"unsupported schema version {version}",
            )
        for migration_version in (1, 2, 3, 4):
            self._verify_migration_checksum(conn, migration_version)
        tables = self._table_names(conn)
        missing = REQUIRED_TABLES - tables
        if missing:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                f"corrupt schema missing tables: {sorted(missing)}",
            )
        indexes = self._index_names(conn)
        missing_idx = REQUIRED_INDEXES - indexes
        if missing_idx:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                f"corrupt schema missing indexes: {sorted(missing_idx)}",
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
            conn.execute(
                """
                INSERT INTO scheduler_repository_reservations(
                    worktree_key, run_id, repository_root, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
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
        except sqlite3.IntegrityError as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "run insert conflict",
            ) from exc

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

    def has_unresolved_abort_hold(self, conn: sqlite3.Connection, run_id: str) -> bool:
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
            SELECT event_id, run_id, sequence, event_kind, event_payload, created_at
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
