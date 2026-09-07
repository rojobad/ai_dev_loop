"""SQLite persistence for the central scheduler engine."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import cast

from ai_dev_loop.scheduler.application.contracts import (
    ReservationStatus,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant, payload_sha256
from ai_dev_loop.scheduler.domain.events import (
    SCHEDULER_EVENT_ADAPTER,
    SchedulerEvent,
    parse_scheduler_event,
)
from ai_dev_loop.scheduler.domain.state import (
    SCHEDULER_STATE_ADAPTER,
    SchedulerState,
    SubmittedState,
    parse_submitted_state,
)

SCHEMA_VERSION = 1
MIGRATION_NAME = "0001_initial"
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
        "idx_scheduler_reservations_run",
    }
)
NON_TERMINAL_STATE_KINDS = frozenset({"queued"})


def _migration_sql() -> str:
    package = resources.files("ai_dev_loop.scheduler.infrastructure.migrations")
    return (package / "0001_initial.sql").read_text(encoding="utf-8")


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


def migration_checksum() -> str:
    return hashlib.sha256(_migration_sql().encode("utf-8")).hexdigest()


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
            for statement in _split_sql_statements(_migration_sql()):
                self._fault_maybe_raise_migration(statement)
                conn.execute(statement)
            checksum = migration_checksum()
            applied_at = encode_utc_instant(datetime.now(tz=UTC))
            conn.execute(
                """
                INSERT INTO scheduler_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (SCHEMA_VERSION, MIGRATION_NAME, checksum, applied_at),
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
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._apply_database_permissions(self.db_path)

    def _fault_maybe_raise_migration(self, statement: str) -> None:
        hook = self._migration_fault_hook
        if hook is not None:
            hook(statement)

    def _verify_current_schema(self, conn: sqlite3.Connection) -> None:
        version = self._user_version(conn)
        if version != SCHEMA_VERSION:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                f"unsupported schema version {version}",
            )
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
        row = conn.execute(
            "SELECT checksum FROM scheduler_schema_migrations WHERE version = ?",
            (SCHEMA_VERSION,),
        ).fetchone()
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                "migration audit row missing for current version",
            )
        if row[0] != migration_checksum():
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA,
                "migration checksum drift detected",
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
        return parse_submitted_state(SCHEDULER_STATE_ADAPTER.validate_json(payload))

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
