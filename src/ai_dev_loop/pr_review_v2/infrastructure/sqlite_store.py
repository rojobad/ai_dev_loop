"""SQLite persistence for the PR review v2 durable engine."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import cast

from ai_dev_loop.pr_review_v2.application.contracts import (
    DispatchStatus,
    LeaseStatus,
    PrReviewEngineError,
    PrReviewEngineErrorKind,
    TimerStatus,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    PR_REVIEW_EFFECT_ADAPTER,
    PrReviewEffect,
    classify_effect,
    parse_pr_review_effect,
)
from ai_dev_loop.pr_review_v2.domain.events import (
    PR_REVIEW_EVENT_ADAPTER,
    PrReviewEvent,
    parse_pr_review_event,
)
from ai_dev_loop.pr_review_v2.domain.state import (
    PR_REVIEW_STATE_ADAPTER,
    PreparedState,
    PrReviewState,
    parse_pr_review_state,
)
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    apply_database_permissions,
    secure_database_path,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import (
    dispatch_id_for,
    encode_utc_instant,
    parse_utc_instant,
    payload_sha256,
    timer_id_for,
)

SCHEMA_VERSION = 1
MIGRATION_NAME = "0001_initial"
REQUIRED_TABLES = frozenset(
    {
        "pr_review_schema_migrations",
        "pr_review_runs",
        "pr_review_events",
        "pr_review_effects",
        "pr_review_timers",
        "pr_review_worker_leases",
    }
)
REQUIRED_INDEXES = frozenset(
    {
        "idx_pr_review_runs_state_kind",
        "idx_pr_review_events_run_seq",
        "idx_pr_review_effects_run_history",
        "idx_pr_review_effects_effect_history",
        "idx_pr_review_effects_eligible",
        "idx_pr_review_effects_claims",
        "idx_pr_review_effects_one_live_per_run",
        "idx_pr_review_timers_eligible",
        "idx_pr_review_timers_one_pending_per_run",
    }
)


def _migration_sql() -> str:
    package = resources.files("ai_dev_loop.pr_review_v2.infrastructure.migrations")
    return (package / "0001_initial.sql").read_text(encoding="utf-8")


def _split_sql_statements(sql: str) -> list[str]:
    """Split migration SQL into executable statements without using executescript."""

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


class SqlitePrReviewStore:
    """One-connection-per-operation SQLite store for PR review v2."""

    def __init__(
        self,
        db_path: Path,
        *,
        busy_timeout_ms: int = 5000,
        migration_fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if busy_timeout_ms < 0:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.VALIDATION,
                "busy_timeout_ms must be >= 0",
            )
        self.db_path = secure_database_path(Path(db_path))
        self.busy_timeout_ms = busy_timeout_ms
        self._migration_fault_hook = migration_fault_hook
        self.bootstrap()

    def bootstrap(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._configure(conn)
            version = self._user_version(conn)
            if version > SCHEMA_VERSION:
                raise PrReviewEngineError(
                    PrReviewEngineErrorKind.SCHEMA,
                    f"database user_version {version} is newer than supported {SCHEMA_VERSION}",
                )
            if version == 0:
                self._bootstrap_v1(conn)
            else:
                self._verify_current_schema(conn)
            apply_database_permissions(self.db_path)

    def _bootstrap_v1(self, conn: sqlite3.Connection) -> None:
        tables = self._table_names(conn)
        if tables - {"sqlite_sequence"}:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.SCHEMA,
                "non-empty v0 database refuses automatic migration",
            )
        try:
            conn.execute("BEGIN IMMEDIATE")
            # executescript() implicitly commits; apply statements inside this txn.
            for statement in _split_sql_statements(_migration_sql()):
                self._fault_maybe_raise_migration(statement)
                conn.execute(statement)
            checksum = migration_checksum()
            applied_at = encode_utc_instant(datetime.now(tz=UTC))
            conn.execute(
                """
                INSERT INTO pr_review_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (SCHEMA_VERSION, MIGRATION_NAME, checksum, applied_at),
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        apply_database_permissions(self.db_path)

    def _fault_maybe_raise_migration(self, statement: str) -> None:
        """Optional hook for deterministic mid-migration crash tests."""

        hook = getattr(self, "_migration_fault_hook", None)
        if hook is not None:
            hook(statement)

    def _verify_current_schema(self, conn: sqlite3.Connection) -> None:
        version = self._user_version(conn)
        if version != SCHEMA_VERSION:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.SCHEMA,
                f"unsupported schema version {version}",
            )
        tables = self._table_names(conn)
        missing = REQUIRED_TABLES - tables
        if missing:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.SCHEMA,
                f"corrupt schema missing tables: {sorted(missing)}",
            )
        indexes = self._index_names(conn)
        missing_idx = REQUIRED_INDEXES - indexes
        if missing_idx:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.SCHEMA,
                f"corrupt schema missing indexes: {sorted(missing_idx)}",
            )
        row = conn.execute(
            "SELECT checksum FROM pr_review_schema_migrations WHERE version = ?",
            (SCHEMA_VERSION,),
        ).fetchone()
        if row is None:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.SCHEMA,
                "migration audit row missing for current version",
            )
        if row[0] != migration_checksum():
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.SCHEMA,
                "migration checksum drift detected",
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            conn = sqlite3.connect(
                self.db_path,
                isolation_level=None,
                timeout=self.busy_timeout_ms / 1000.0,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.INTERNAL,
                f"failed to open database: {exc.__class__.__name__}",
            ) from exc
        conn.row_factory = sqlite3.Row
        try:
            self._configure(conn)
            yield conn
        finally:
            conn.close()
            if self.db_path.exists():
                apply_database_permissions(self.db_path)

    def _configure(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")

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
                    raise PrReviewEngineError(
                        PrReviewEngineErrorKind.BUSY,
                        "database is busy",
                    ) from exc
                raise PrReviewEngineError(
                    PrReviewEngineErrorKind.INTERNAL,
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

    # --- serialization helpers -------------------------------------------------

    @staticmethod
    def dump_state(state: PrReviewState) -> tuple[str, str, str]:
        validated = PR_REVIEW_STATE_ADAPTER.validate_python(state.model_dump(mode="json"))
        text = PR_REVIEW_STATE_ADAPTER.dump_json(validated).decode("utf-8")
        return validated.kind, text, payload_sha256(text)

    @staticmethod
    def load_state(payload: str) -> PrReviewState:
        return parse_pr_review_state(PR_REVIEW_STATE_ADAPTER.validate_json(payload))

    @staticmethod
    def dump_event(event: PrReviewEvent) -> tuple[str, str, str]:
        validated = PR_REVIEW_EVENT_ADAPTER.validate_python(event.model_dump(mode="json"))
        text = PR_REVIEW_EVENT_ADAPTER.dump_json(validated).decode("utf-8")
        return validated.kind, text, payload_sha256(text)

    @staticmethod
    def load_event(payload: str) -> PrReviewEvent:
        return parse_pr_review_event(PR_REVIEW_EVENT_ADAPTER.validate_json(payload))

    @staticmethod
    def dump_effect(effect: PrReviewEffect) -> tuple[str, str, str]:
        validated = PR_REVIEW_EFFECT_ADAPTER.validate_python(effect.model_dump(mode="json"))
        text = PR_REVIEW_EFFECT_ADAPTER.dump_json(validated).decode("utf-8")
        return validated.kind, text, payload_sha256(text)

    @staticmethod
    def load_effect(payload: str) -> PrReviewEffect:
        return parse_pr_review_effect(PR_REVIEW_EFFECT_ADAPTER.validate_json(payload))

    # --- run / snapshot --------------------------------------------------------

    def insert_prepared_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        state: PreparedState,
        now: datetime,
    ) -> None:
        if state.run_id != run_id:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.VALIDATION,
                "PreparedState.run_id must equal requested run_id",
            )
        kind, payload, digest = self.dump_state(state)
        if kind != "prepared":
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.VALIDATION,
                "create_run accepts only PreparedState",
            )
        now_text = encode_utc_instant(now)
        try:
            conn.execute(
                """
                INSERT INTO pr_review_runs(
                    run_id, state_kind, state_payload, state_payload_sha256,
                    version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (run_id, kind, payload, digest, now_text, now_text),
            )
            conn.execute(
                """
                INSERT INTO pr_review_worker_leases(
                    run_id, owner_id, generation, status,
                    acquired_at, heartbeat_at, expires_at, updated_at
                ) VALUES (?, NULL, 0, ?, NULL, NULL, NULL, ?)
                """,
                (run_id, LeaseStatus.INACTIVE.value, now_text),
            )
        except sqlite3.IntegrityError as exc:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CONFLICT,
                "run_id already exists",
            ) from exc

    def get_run_row(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM pr_review_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.NOT_FOUND,
                f"run not found: {run_id}",
            )
        return cast(sqlite3.Row, row)

    def load_validated_snapshot(
        self, conn: sqlite3.Connection, run_id: str
    ) -> tuple[PrReviewState, int, datetime]:
        row = self.get_run_row(conn, run_id)
        state = self.load_state(row["state_payload"])
        if state.kind != row["state_kind"]:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "state_kind projection disagrees with payload",
            )
        if payload_sha256(row["state_payload"]) != row["state_payload_sha256"]:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "state payload hash mismatch",
            )
        if state.run_id != run_id:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "state.run_id disagrees with primary key",
            )
        return state, int(row["version"]), parse_utc_instant(row["updated_at"])

    def cas_update_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        observed_version: int,
        new_state: PrReviewState,
        now: datetime,
    ) -> int:
        kind, payload, digest = self.dump_state(new_state)
        if kind != new_state.kind:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "serialized state kind mismatch",
            )
        resulting = observed_version + 1
        cur = conn.execute(
            """
            UPDATE pr_review_runs
            SET state_kind = ?, state_payload = ?, state_payload_sha256 = ?,
                version = ?, updated_at = ?
            WHERE run_id = ? AND version = ?
            """,
            (
                kind,
                payload,
                digest,
                resulting,
                encode_utc_instant(now),
                run_id,
                observed_version,
            ),
        )
        if cur.rowcount != 1:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "snapshot CAS affected unexpected row count",
            )
        return resulting

    def next_event_sequence(self, conn: sqlite3.Connection, run_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS max_seq FROM pr_review_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["max_seq"]) + 1

    def find_event_by_id(self, conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
        row = conn.execute(
            "SELECT * FROM pr_review_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def insert_event_row(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        run_id: str,
        sequence: int,
        event: PrReviewEvent,
        disposition: str,
        expected_run_version: int | None,
        observed_run_version: int,
        resulting_run_version: int | None,
        rejection_code: str | None,
        safe_detail: str | None,
        now: datetime,
        resulting_state: PrReviewState | None = None,
    ) -> None:
        kind, payload, digest = self.dump_event(event)
        resulting_state_payload = None
        resulting_state_digest = None
        if resulting_state is not None:
            _, resulting_state_payload, resulting_state_digest = self.dump_state(resulting_state)
        conn.execute(
            """
            INSERT INTO pr_review_events(
                event_id, run_id, sequence, event_kind, event_payload,
                event_payload_sha256, disposition, expected_run_version,
                observed_run_version, resulting_run_version,
                resulting_state_payload, resulting_state_payload_sha256,
                rejection_code, safe_detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                run_id,
                sequence,
                kind,
                payload,
                digest,
                disposition,
                expected_run_version,
                observed_run_version,
                resulting_run_version,
                resulting_state_payload,
                resulting_state_digest,
                rejection_code,
                safe_detail,
                encode_utc_instant(now),
            ),
        )

    def insert_effect_dispatches(
        self,
        conn: sqlite3.Connection,
        *,
        source_event_id: str,
        run_id: str,
        effects: tuple[PrReviewEffect, ...],
        now: datetime,
        claimed_run_version: int | None = None,
    ) -> list[str]:
        """Insert emitted effects. claimed_run_version is the resulting snapshot version."""

        now_text = encode_utc_instant(now)
        dispatch_ids: list[str] = []
        for ordinal, effect in enumerate(effects):
            kind, payload, digest = self.dump_effect(effect)
            if effect.kind != kind:
                raise PrReviewEngineError(
                    PrReviewEngineErrorKind.CORRUPTION,
                    "effect kind projection mismatch",
                )
            classification = classify_effect(effect).value
            available_at = now_text
            not_before = getattr(effect, "not_before", None)
            if effect.kind == "observe_bot_review" and not_before is not None:
                available_at = encode_utc_instant(not_before)
            dispatch_id = dispatch_id_for(source_event_id, ordinal)
            conn.execute(
                """
                INSERT INTO pr_review_effects(
                    dispatch_id, source_event_id, effect_ordinal, run_id,
                    effect_id, idempotency_key, effect_kind, effect_payload,
                    effect_payload_sha256, classification, attempt, max_attempts,
                    status, available_at, claimed_run_version, claim_id,
                    claim_owner_id, claim_lease_generation, claimed_at, completed_at,
                    last_error_kind, last_error_summary, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                    NULL, NULL, NULL, NULL, NULL, NULL, ?, ?
                )
                """,
                (
                    dispatch_id,
                    source_event_id,
                    ordinal,
                    run_id,
                    effect.effect_id,
                    effect.idempotency_key,
                    kind,
                    payload,
                    digest,
                    classification,
                    effect.attempt,
                    effect.max_attempts,
                    DispatchStatus.PENDING.value,
                    available_at,
                    now_text,
                    now_text,
                ),
            )
            # Store expected completion version in a side channel: we keep it on claim.
            # The resulting snapshot version is recorded when claiming via claimed_run_version.
            _ = claimed_run_version
            dispatch_ids.append(dispatch_id)
        return dispatch_ids

    def reconcile_retry_timer(
        self,
        conn: sqlite3.Connection,
        *,
        source_event_id: str,
        run_id: str,
        new_state: PrReviewState,
        resulting_version: int,
        now: datetime,
    ) -> None:
        now_text = encode_utc_instant(now)
        # Supersede any pending timers when leaving waiting_retry or replacing them.
        conn.execute(
            """
            UPDATE pr_review_timers
            SET status = ?, updated_at = ?
            WHERE run_id = ? AND status = ?
            """,
            (
                TimerStatus.SUPERSEDED.value,
                now_text,
                run_id,
                TimerStatus.PENDING.value,
            ),
        )
        if new_state.kind != "waiting_retry":
            return
        timer_id = timer_id_for(source_event_id)
        conn.execute(
            """
            INSERT INTO pr_review_timers(
                timer_id, source_event_id, run_id, timer_kind, due_at,
                target_effect_id, expected_run_version, status, fired_event_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'retry_due', ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                timer_id,
                source_event_id,
                run_id,
                encode_utc_instant(new_state.next_attempt_at),
                new_state.retrying_effect_id,
                resulting_version,
                TimerStatus.PENDING.value,
                now_text,
                now_text,
            ),
        )

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
            UPDATE pr_review_effects
            SET status = ?, updated_at = ?,
                claim_id = NULL, claim_owner_id = NULL,
                claim_lease_generation = NULL, claimed_at = NULL
            WHERE run_id = ? AND status IN (?, ?, ?)
            """,
            (
                DispatchStatus.CANCELLED.value,
                now_text,
                run_id,
                DispatchStatus.PENDING.value,
                DispatchStatus.CLAIMED.value,
                DispatchStatus.RETRY_WAIT.value,
            ),
        )
        conn.execute(
            """
            UPDATE pr_review_timers
            SET status = ?, updated_at = ?
            WHERE run_id = ? AND status = ?
            """,
            (
                TimerStatus.CANCELLED.value,
                now_text,
                run_id,
                TimerStatus.PENDING.value,
            ),
        )

    def invalidate_lease(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        now: datetime,
        status: LeaseStatus = LeaseStatus.ABORTED,
    ) -> None:
        conn.execute(
            """
            UPDATE pr_review_worker_leases
            SET status = ?, owner_id = NULL, updated_at = ?,
                heartbeat_at = NULL, expires_at = NULL
            WHERE run_id = ?
            """,
            (status.value, encode_utc_instant(now), run_id),
        )

    def get_lease_row(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM pr_review_worker_leases WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "lease row missing",
            )
        return cast(sqlite3.Row, row)

    def list_due_timer_ids(self, conn: sqlite3.Connection, now: datetime) -> list[str]:
        rows = conn.execute(
            """
            SELECT timer_id FROM pr_review_timers
            WHERE status = ? AND due_at <= ?
            ORDER BY due_at ASC, timer_id ASC
            """,
            (TimerStatus.PENDING.value, encode_utc_instant(now)),
        ).fetchall()
        return [str(row["timer_id"]) for row in rows]

    def list_due_timer_ids_for_run(
        self, conn: sqlite3.Connection, run_id: str, now: datetime
    ) -> list[str]:
        rows = conn.execute(
            """
            SELECT timer_id FROM pr_review_timers
            WHERE run_id = ? AND status = ? AND due_at <= ?
            ORDER BY due_at ASC, timer_id ASC
            """,
            (run_id, TimerStatus.PENDING.value, encode_utc_instant(now)),
        ).fetchall()
        return [str(row["timer_id"]) for row in rows]

    def list_nonterminal_run_rows(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        rows = conn.execute(
            """
            SELECT * FROM pr_review_runs
            WHERE state_kind NOT IN ('completed', 'failed', 'aborted')
            ORDER BY created_at ASC, run_id ASC
            """
        ).fetchall()
        return [cast(sqlite3.Row, row) for row in rows]

    def list_events_for_run(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        *,
        limit: int,
        newest_first: bool,
    ) -> list[sqlite3.Row]:
        if limit < 1:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.VALIDATION,
                "history limit must be positive",
            )
        order = "DESC" if newest_first else "ASC"
        rows = conn.execute(
            f"""
            SELECT * FROM pr_review_events
            WHERE run_id = ?
            ORDER BY sequence {order}
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        return [cast(sqlite3.Row, row) for row in rows]

    def get_timer_row(self, conn: sqlite3.Connection, timer_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            conn.execute(
                "SELECT * FROM pr_review_timers WHERE timer_id = ?",
                (timer_id,),
            ).fetchone(),
        )

    def get_live_dispatch(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            conn.execute(
                """
                SELECT * FROM pr_review_effects
                WHERE run_id = ? AND status IN (?, ?)
                ORDER BY created_at ASC, dispatch_id ASC
                LIMIT 1
                """,
                (run_id, DispatchStatus.PENDING.value, DispatchStatus.CLAIMED.value),
            ).fetchone(),
        )

    def get_dispatch(self, conn: sqlite3.Connection, dispatch_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM pr_review_effects WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        if row is None:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.NOT_FOUND,
                "dispatch not found",
            )
        return cast(sqlite3.Row, row)

    def load_validated_effect(self, row: sqlite3.Row) -> PrReviewEffect:
        effect = self.load_effect(row["effect_payload"])
        if effect.kind != row["effect_kind"]:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "effect_kind projection mismatch",
            )
        if effect.effect_id != row["effect_id"]:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "effect_id projection mismatch",
            )
        if effect.idempotency_key != row["idempotency_key"]:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "idempotency_key projection mismatch",
            )
        if effect.attempt != int(row["attempt"]):
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "attempt projection mismatch",
            )
        if effect.max_attempts != int(row["max_attempts"]):
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "max_attempts projection mismatch",
            )
        expected_classification = classify_effect(effect).value
        if expected_classification != row["classification"]:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "classification projection mismatch",
            )
        if effect.run_id != row["run_id"]:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "effect run_id projection mismatch",
            )
        if payload_sha256(row["effect_payload"]) != row["effect_payload_sha256"]:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "effect payload hash mismatch",
            )
        return effect
