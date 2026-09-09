"""Explicit destructive cleanup of retired legacy XDG state roots."""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.errors import LockError, ValidationError
from ai_dev_loop.locking import FileLock, LockMetadata, is_process_alive, read_lock_metadata
from ai_dev_loop.paths import state_dir
from ai_dev_loop.scheduler.application.tick_fencing import parse_utc_instant
from ai_dev_loop.scheduler.infrastructure.paths import engine_db_path_for_state_dir
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    ATTEMPT_STATUS_ACTIVE,
    ATTEMPT_STATUS_LAUNCHING,
    ATTEMPT_STATUS_UNCERTAIN,
    SqliteSchedulerStore,
)

LEGACY_STATE_CHILD_NAMES: tuple[str, ...] = ("runs", "pr-review-v2")
CUTOVER_CONFIRMATION_TOKEN = "delete-legacy-state"
CUTOVER_TICK_OWNER = "cutover-cleanup"
CUTOVER_TICK_LEASE_SECONDS = 300


@dataclass(frozen=True)
class LegacyTargetValidation:
    child_name: str
    resolved_path: Path
    exists: bool


@dataclass(frozen=True)
class CutoverCleanupResult:
    resolved_state_root: str
    deleted_paths: tuple[str, ...]
    already_absent: tuple[str, ...]
    dry_run: bool


@dataclass
class _CutoverLeaseHolder:
    store: SqliteSchedulerStore
    generation: int

    def renew_lease(self, now: datetime | None = None) -> None:
        current = now if now is not None else datetime.now(UTC)
        with self.store.begin_immediate() as conn:
            acquired = self.store.acquire_global_tick_lease(
                conn,
                owner_id=CUTOVER_TICK_OWNER,
                now=current,
                ttl_seconds=CUTOVER_TICK_LEASE_SECONDS,
            )
            if acquired is None:
                raise ValidationError("cleanup refused: failed to renew cutover tick lease")
            self.generation = acquired[0]


def _reject_ambiguous_path_component(path: Path) -> None:
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationError("scheduler state path contains ambiguous components; cleanup refused")


def _reject_symlink_ancestors(path: Path) -> None:
    """Reject paths whose traversal crosses a symlink before resolve() erases it."""

    parts = path.parts
    if not parts:
        return
    if path.is_absolute():
        probe = Path(parts[0])
        start = 1
    else:
        probe = Path(".")
        start = 0
    for part in parts[start:]:
        if part in {".", ".."}:
            raise ValidationError(
                "scheduler state path contains ambiguous components; cleanup refused"
            )
        probe = probe / part
        if probe.is_symlink():
            raise ValidationError(
                f"scheduler state path component {probe} is a symlink; cleanup refused"
            )


def validate_state_root(state_root: Path | None = None) -> Path:
    """Validate the scheduler state root before resolve() erases symlink information."""

    raw_root = state_root if state_root is not None else state_dir()
    _reject_ambiguous_path_component(raw_root)
    _reject_symlink_ancestors(raw_root)
    if raw_root.is_symlink():
        raise ValidationError("scheduler state root is a symlink; cleanup refused")
    resolved_root = raw_root.resolve(strict=False)
    if not resolved_root.is_dir():
        raise ValidationError("resolved scheduler state root is missing or not a directory")
    if state_root is None:
        canonical_raw = state_dir()
        _reject_ambiguous_path_component(canonical_raw)
        _reject_symlink_ancestors(canonical_raw)
        if canonical_raw.is_symlink():
            raise ValidationError("scheduler state root is a symlink; cleanup refused")
        canonical_resolved = canonical_raw.resolve(strict=False)
        if canonical_resolved != resolved_root:
            raise ValidationError("scheduler state root is ambiguous; cleanup refused")
    return resolved_root


def resolve_engine_db_path(resolved_root: Path, db_path: Path | None = None) -> Path:
    """Bind scheduler ledger checks to the validated cleanup state root."""

    if db_path is not None:
        return db_path
    return engine_db_path_for_state_dir(resolved_root)


def cutover_target_paths(state_root: Path | None = None) -> tuple[Path, tuple[str, ...]]:
    resolved_root = validate_state_root(state_root)
    return resolved_root, tuple(
        str(resolved_root / child_name) for child_name in LEGACY_STATE_CHILD_NAMES
    )


def resolve_legacy_target(state_root: Path, child_name: str) -> LegacyTargetValidation:
    if child_name not in LEGACY_STATE_CHILD_NAMES:
        raise ValidationError(
            f"unsupported legacy state child {child_name!r}; "
            f"allowed values: {', '.join(LEGACY_STATE_CHILD_NAMES)}"
        )
    resolved_root = validate_state_root(state_root)
    direct_target = resolved_root / child_name
    if direct_target.is_symlink():
        raise ValidationError(f"legacy target {child_name!r} is a symlink; cleanup refused")
    if direct_target.exists() and not direct_target.is_dir():
        raise ValidationError(f"legacy target {child_name!r} is not a directory; cleanup refused")
    return LegacyTargetValidation(
        child_name=child_name,
        resolved_path=direct_target,
        exists=direct_target.exists(),
    )


def _scheduler_active_work_reason_conn(
    conn: sqlite3.Connection,
    store: SqliteSchedulerStore,
    current: datetime,
) -> str | None:
    lease = store.get_tick_lease_row(conn)
    if lease["status"] == "active" and lease["expires_at"] is not None:
        expires_at = parse_utc_instant(str(lease["expires_at"]))
        if expires_at > current and lease["owner_id"] != CUTOVER_TICK_OWNER:
            return "active scheduler tick lease"
    row = conn.execute(
        """
        SELECT COUNT(*) FROM scheduler_attempts
        WHERE status IN (?, ?, ?)
        """,
        (
            ATTEMPT_STATUS_LAUNCHING,
            ATTEMPT_STATUS_ACTIVE,
            ATTEMPT_STATUS_UNCERTAIN,
        ),
    ).fetchone()
    if row is not None and int(row[0]) > 0:
        return "active scheduler attempt"
    capacity = store.get_capacity_row(conn)
    if capacity["holder_run_id"] is not None:
        return "scheduler capacity holder is active"
    return None


def scheduler_active_work_reason(
    *,
    resolved_state_root: Path | None = None,
    engine_path: Path | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return a blocking reason when scheduler work is active for the bound ledger."""

    bound_root = validate_state_root(resolved_state_root)
    ledger_path = resolve_engine_db_path(bound_root, engine_path)
    if not ledger_path.is_file():
        return None
    current = now or datetime.now(UTC)
    store = SqliteSchedulerStore.open_readonly(ledger_path)
    with store._connect() as conn:
        return _scheduler_active_work_reason_conn(conn, store, current)


def _cutover_lock_path(resolved_root: Path) -> Path:
    return resolved_root / "locks" / "cutover.cleanup.lock"


def cutover_cleanup_blocks_scheduler_tick(
    resolved_state_root: Path | None = None,
) -> bool:
    """Return True when a live cutover cleanup holds the advisory deletion lock."""

    root = (
        resolved_state_root.resolve(strict=False)
        if resolved_state_root is not None
        else state_dir().resolve(strict=False)
    )
    lock_path = _cutover_lock_path(root)
    if not lock_path.is_file():
        return False
    metadata = read_lock_metadata(lock_path)
    if metadata is None or metadata.run_id != CUTOVER_TICK_OWNER:
        return False
    if not is_process_alive(metadata.pid):
        return False
    try:
        import fcntl

        handle = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
        finally:
            handle.close()
    except OSError:
        return True


@contextmanager
def _cutover_coordination(
    resolved_root: Path,
    engine_path: Path,
    now: datetime,
) -> Iterator[_CutoverLeaseHolder]:
    lock = FileLock(_cutover_lock_path(resolved_root))
    metadata = LockMetadata(
        pid=os.getpid(),
        run_id=CUTOVER_TICK_OWNER,
        repository_path=str(resolved_root),
        started_at=now,
    )
    try:
        lock.acquire(metadata)
    except LockError as exc:
        raise ValidationError("cleanup refused: concurrent cutover cleanup in progress") from exc

    store = SqliteSchedulerStore(engine_path)
    with store.begin_immediate() as conn:
        reason = _scheduler_active_work_reason_conn(conn, store, now)
        if reason is not None:
            lock.release()
            raise ValidationError(f"cleanup refused: {reason}")
        acquired = store.acquire_global_tick_lease(
            conn,
            owner_id=CUTOVER_TICK_OWNER,
            now=now,
            ttl_seconds=CUTOVER_TICK_LEASE_SECONDS,
        )
        if acquired is None:
            lock.release()
            raise ValidationError("cleanup refused: active scheduler tick lease")
        holder = _CutoverLeaseHolder(store=store, generation=acquired[0])

    try:
        yield holder
    finally:
        release_now = datetime.now(UTC)
        with holder.store.begin_immediate() as conn:
            holder.store.release_global_tick_lease(
                conn,
                owner_id=CUTOVER_TICK_OWNER,
                generation=holder.generation,
                now=release_now,
            )
        lock.release()


def _delete_legacy_tree(path: Path) -> None:
    shutil.rmtree(path)


def run_cutover_cleanup(
    *,
    confirmation_token: str,
    dry_run: bool = False,
    state_root: Path | None = None,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> CutoverCleanupResult:
    if confirmation_token != CUTOVER_CONFIRMATION_TOKEN:
        raise ValidationError(f"confirmation token must be exactly {CUTOVER_CONFIRMATION_TOKEN!r}")

    resolved_root = validate_state_root(state_root)
    engine_path = resolve_engine_db_path(resolved_root, db_path)
    validations = [
        resolve_legacy_target(resolved_root, child) for child in LEGACY_STATE_CHILD_NAMES
    ]

    current = now or datetime.now(UTC)
    if dry_run:
        active_reason = scheduler_active_work_reason(
            resolved_state_root=resolved_root,
            engine_path=engine_path,
            now=current,
        )
        if active_reason is not None:
            raise ValidationError(f"cleanup refused: {active_reason}")
        dry_deleted = [str(item.resolved_path) for item in validations if item.exists]
        dry_absent = [str(item.resolved_path) for item in validations if not item.exists]
        return CutoverCleanupResult(
            resolved_state_root=str(resolved_root),
            deleted_paths=tuple(dry_deleted),
            already_absent=tuple(dry_absent),
            dry_run=True,
        )

    deleted_paths: list[str] = []
    absent_paths: list[str] = []
    with _cutover_coordination(resolved_root, engine_path, current) as holder:
        with holder.store.begin_immediate() as conn:
            reason = _scheduler_active_work_reason_conn(conn, holder.store, current)
            if reason is not None:
                raise ValidationError(f"cleanup refused: {reason}")
        for item in validations:
            holder.renew_lease()
            path_text = str(item.resolved_path)
            if not item.exists:
                absent_paths.append(path_text)
                continue
            _delete_legacy_tree(item.resolved_path)
            deleted_paths.append(path_text)

    return CutoverCleanupResult(
        resolved_state_root=str(resolved_root),
        deleted_paths=tuple(deleted_paths),
        already_absent=tuple(absent_paths),
        dry_run=False,
    )


__all__ = [
    "CUTOVER_CONFIRMATION_TOKEN",
    "CutoverCleanupResult",
    "LEGACY_STATE_CHILD_NAMES",
    "LegacyTargetValidation",
    "cutover_cleanup_blocks_scheduler_tick",
    "cutover_target_paths",
    "resolve_engine_db_path",
    "resolve_legacy_target",
    "run_cutover_cleanup",
    "scheduler_active_work_reason",
    "validate_state_root",
]
