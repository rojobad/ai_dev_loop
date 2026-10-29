"""Explicit bounded historical usage import. Reads do not call this module."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError

from ai_dev_loop.errors import ValidationError as AppValidationError
from ai_dev_loop.integration_api.process_output_auth import (
    ProcessOutputIntegrityError,
    authenticate_process_attempt,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.token_usage_query import (
    _INFERENCE_EFFECTS,
    _Attempt,
    _attempt_from_authenticated,
    _historical_codex_observations,
    _load_published_observations,
    _published_contribution_complete,
    _validated_observations,
)
from ai_dev_loop.scheduler.application.token_usage_query import (
    _coverage as _provider_coverage,
)
from ai_dev_loop.scheduler.application.usage_ingestion import (
    _binding,
    _FactWrite,
    _fence,
    _observation_write,
    _ObservationWrite,
    _origin,
    _terminal_status,
    _write_classified_without_counters,
    merge_usage_snapshot,
)
from ai_dev_loop.scheduler.application.usage_ingestion import (
    _coverage as _fact_coverage,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.token_usage import UsageCoverage, UsageObservationV1
from ai_dev_loop.scheduler.domain.usage_facts import (
    UsageFactPayloadV1,
    UsageSettlement,
    canonical_json,
    canonical_sha256,
    safe_diagnostic_codes,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_artifact_root, default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactError
from ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts import (
    ReadOnlyProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_MAX_BOUND = 100_000
_DEFAULT_BATCH_SIZE = 32
_DEFAULT_MAX_ATTEMPTS = 100
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_TERMINAL = frozenset({"completed", "failed", "cancelled", "uncertain"})
_EFFECT_SQL = tuple(sorted(_INFERENCE_EFFECTS))
_LOSS_CODES = frozenset({"usage_source_debt", "usage_source_missing"})


class UsageBackfillBarrier(Exception):
    """Raised after a fact commit and before the job cursor advances."""


class _SnapshotRow:
    def __init__(self, data: Mapping[str, object]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def keys(self) -> list[str]:
        return list(self._data)


@dataclass(frozen=True)
class UsageBackfillRequest:
    mode: Literal["check", "apply"]
    scope_kind: Literal["all", "run", "project"]
    scope_value: str | None
    batch_size: int = _DEFAULT_BATCH_SIZE
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS
    resume_job_id: str | None = None
    scope_explicit: bool = True


@dataclass(frozen=True)
class UsageBackfillReport:
    mode: Literal["check", "apply"]
    scope_kind: Literal["all", "run", "project"]
    scope_value: str | None
    job_id: str | None
    job_status: Literal["not_started", "running", "completed", "stopped"]
    eligible_terminal: int
    materialized: int
    known: int
    partial: int
    unknown: int
    unattributed: int
    unmaterialized: int
    active_unsettled: int
    missing_sources: int
    corrupt_sources: int
    malformed_sources: int
    conflicting: int
    lost_source: int
    provider_coverage: UsageCoverage
    materialization: Literal["complete", "incomplete"]
    statistically_ready: bool
    remaining: int
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class _ReadyImport:
    fact: _FactWrite
    observations: tuple[_ObservationWrite, ...]


@dataclass(frozen=True)
class _ClassifiedImport:
    kind: Literal["missing", "corrupt", "malformed"]
    coverage: Literal["complete", "partial", "unknown", "not_applicable"] | None
    settlement: UsageSettlement
    diagnostic: str | None
    extra_diagnostics: tuple[str, ...]
    configured_model: str | None


def _valid_bound(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _MAX_BOUND


def parse_usage_backfill_request(
    *,
    all_runs: bool,
    run_id: str | None,
    project_name: str | None,
    check: bool,
    apply: bool,
    batch_size: int,
    max_attempts: int,
    resume_job_id: str | None,
) -> UsageBackfillRequest:
    """Validate the explicit scope and mode before any ledger access."""

    scopes = [all_runs, run_id is not None, project_name is not None]
    if resume_job_id is not None and not check and not apply:
        apply = True
    if check == apply:
        raise AppValidationError("usage backfill requires exactly one of --check or --apply")
    if resume_job_id is not None and check:
        raise AppValidationError("usage backfill --resume cannot be combined with --check")
    explicit_scopes = sum(bool(item) for item in scopes)
    if explicit_scopes > 1:
        raise AppValidationError("usage backfill scopes conflict")
    if explicit_scopes != 1 and resume_job_id is None:
        raise AppValidationError(
            "usage backfill requires exactly one of --all, --run-id, or --project-name"
        )
    if not _valid_bound(batch_size) or not _valid_bound(max_attempts):
        raise AppValidationError("usage backfill bounds must be integers from 1 to 100000")
    if resume_job_id is not None and _JOB_ID.fullmatch(resume_job_id) is None:
        raise AppValidationError("usage backfill job id is invalid")
    scope_kind: Literal["all", "run", "project"]
    scope_value: str | None
    if run_id is not None:
        if not run_id.strip() or run_id != run_id.strip() or any(char.isspace() for char in run_id):
            raise AppValidationError("usage backfill run id is invalid")
        scope_kind, scope_value = "run", run_id
    elif project_name is not None:
        if (
            not project_name.strip()
            or project_name != project_name.strip()
            or any(ord(char) < 32 for char in project_name)
        ):
            raise AppValidationError("usage backfill project name is invalid")
        scope_kind, scope_value = "project", project_name
    elif all_runs or resume_job_id is not None:
        scope_kind, scope_value = "all", None
    else:
        raise AppValidationError(
            "usage backfill requires exactly one of --all, --run-id, or --project-name"
        )
    return UsageBackfillRequest(
        mode="check" if check else "apply",
        scope_kind=scope_kind,
        scope_value=scope_value,
        batch_size=batch_size,
        max_attempts=max_attempts,
        resume_job_id=resume_job_id,
        scope_explicit=explicit_scopes == 1,
    )


def run_usage_backfill(
    request: UsageBackfillRequest,
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
    now: datetime | None = None,
    before_commit: Callable[[str], None] | None = None,
    barrier: str | None = None,
) -> UsageBackfillReport:
    """Check readiness or apply one bounded import. Check never migrates."""

    database = db_path or default_engine_db_path()
    if not database.is_file():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.NOT_FOUND,
            "scheduler database not found",
        )
    moment = now or datetime.now(tz=UTC)
    artifacts = ReadOnlyProtectedArtifactStore(artifact_root or default_artifact_root())
    if request.mode == "check":
        store = SqliteSchedulerStore.open_readonly(database)
        with store.begin_read() as conn:
            _require_known_scope(conn, request)
        return _readiness(store, artifacts, request, job_id=None, job_status="not_started")
    store = SqliteSchedulerStore(database)
    store.usage_backfill_barrier = barrier
    job_id = _prepare_job(store, request, now_text=encode_utc_instant(moment))
    _apply_job(
        store,
        artifacts,
        job_id=job_id,
        batch_size=request.batch_size,
        max_attempts=request.max_attempts,
        now=moment,
        before_commit=before_commit,
    )
    with store.begin_read() as conn:
        job = _job_row(conn, job_id)
        scope_kind = cast(Literal["all", "run", "project"], str(job["scope_kind"]))
        scope_value = None if job["scope_value"] is None else str(job["scope_value"])
        status = cast(Literal["running", "completed", "stopped"], str(job["status"]))
    scoped = UsageBackfillRequest(
        mode="apply",
        scope_kind=scope_kind,
        scope_value=scope_value,
        batch_size=request.batch_size,
        max_attempts=request.max_attempts,
    )
    return _readiness(store, artifacts, scoped, job_id=job_id, job_status=status)


def render_backfill_report(report: UsageBackfillReport, *, output: str) -> str:
    payload = {
        "mode": report.mode,
        "scope_kind": report.scope_kind,
        "scope_value": report.scope_value,
        "job_id": report.job_id,
        "job_status": report.job_status,
        "eligible_terminal": report.eligible_terminal,
        "materialized": report.materialized,
        "known": report.known,
        "partial": report.partial,
        "unknown": report.unknown,
        "unattributed": report.unattributed,
        "unmaterialized": report.unmaterialized,
        "active_unsettled": report.active_unsettled,
        "missing_sources": report.missing_sources,
        "corrupt_sources": report.corrupt_sources,
        "malformed_sources": report.malformed_sources,
        "conflicting": report.conflicting,
        "lost_source": report.lost_source,
        "provider_coverage": report.provider_coverage,
        "materialization": report.materialization,
        "statistically_ready": report.statistically_ready,
        "remaining": report.remaining,
        "diagnostics": list(report.diagnostics),
    }
    if output == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    total = "ready" if report.statistically_ready else "not_ready"
    job_id = report.job_id or "-"
    return (
        f"usage backfill {report.mode} scope={report.scope_kind} status={total}\n"
        f"job_id={job_id} job_status={report.job_status}\n"
        f"materialization={report.materialization} "
        f"provider_coverage={report.provider_coverage}\n"
        f"eligible_terminal={report.eligible_terminal} materialized={report.materialized} "
        f"unmaterialized={report.unmaterialized} active_unsettled={report.active_unsettled}\n"
        f"known={report.known} partial={report.partial} unknown={report.unknown} "
        f"unattributed={report.unattributed} conflicting={report.conflicting}\n"
        f"missing_sources={report.missing_sources} corrupt_sources={report.corrupt_sources} "
        f"malformed_sources={report.malformed_sources} lost_source={report.lost_source}\n"
        f"remaining={report.remaining}\n"
    )


def _require_known_scope(conn: sqlite3.Connection, request: UsageBackfillRequest) -> None:
    if request.scope_kind == "run":
        row = conn.execute(
            "SELECT 1 FROM scheduler_runs WHERE run_id = ?",
            (request.scope_value,),
        ).fetchone()
        if row is None:
            raise SchedulerEngineError(SchedulerEngineErrorKind.NOT_FOUND, "run not found")
    elif request.scope_kind == "project":
        row = conn.execute(
            """
            SELECT 1 FROM scheduler_runs
            WHERE json_extract(state_payload, '$.context.project_name') = ?
            LIMIT 1
            """,
            (request.scope_value,),
        ).fetchone()
        if row is None:
            raise SchedulerEngineError(SchedulerEngineErrorKind.NOT_FOUND, "project not found")


def _prepare_job(
    store: SqliteSchedulerStore,
    request: UsageBackfillRequest,
    *,
    now_text: str,
) -> str:
    with store.begin_immediate() as conn:
        if request.resume_job_id is not None:
            job = _job_row(conn, request.resume_job_id)
            if request.scope_explicit:
                same_kind = str(job["scope_kind"]) == request.scope_kind
                same_value = (job["scope_value"] is None and request.scope_value is None) or str(
                    job["scope_value"] or ""
                ) == str(request.scope_value or "")
                if not same_kind or not same_value:
                    raise SchedulerEngineError(
                        SchedulerEngineErrorKind.CONFLICT,
                        "usage backfill resume scope does not match the job",
                    )
            if int(job["batch_size"]) != request.batch_size or int(job["max_attempts"]) != (
                request.max_attempts
            ):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "usage backfill resume bounds do not match the job",
                )
            return str(job["job_id"])
        _require_known_scope(conn, request)
        job_id = uuid.uuid4().hex
        rows = _eligible_rows(conn, request)
        conn.execute(
            """
            INSERT INTO scheduler_usage_backfill_jobs(
                job_id, scope_kind, scope_value, status, batch_size, max_attempts,
                high_water, snapshot_count, created_at, updated_at
            ) VALUES (?, ?, ?, 'running', ?, ?, 0, ?, ?, ?)
            """,
            (
                job_id,
                request.scope_kind,
                request.scope_value,
                request.batch_size,
                request.max_attempts,
                len(rows),
                now_text,
                now_text,
            ),
        )
        for ordinal, row in enumerate(rows):
            conn.execute(
                """
                INSERT INTO scheduler_usage_backfill_items(
                    job_id, ordinal, attempt_id, run_id, revision, status, diagnostic_codes
                ) VALUES (?, ?, ?, ?, ?, 'pending', '[]')
                """,
                (
                    job_id,
                    ordinal,
                    str(row["attempt_id"]),
                    str(row["run_id"]),
                    _item_revision(
                        row,
                        publishers=_terminal_reconcile_publishers(
                            store, conn, str(row["attempt_id"])
                        ),
                    ),
                ),
            )
        if not rows:
            conn.execute(
                """
                UPDATE scheduler_usage_backfill_jobs
                SET status = 'completed', updated_at = ?
                WHERE job_id = ?
                """,
                (now_text, job_id),
            )
        return job_id


def _apply_job(
    store: SqliteSchedulerStore,
    artifacts: ReadOnlyProtectedArtifactStore,
    *,
    job_id: str,
    batch_size: int,
    max_attempts: int,
    now: datetime,
    before_commit: Callable[[str], None] | None,
) -> None:
    processed = 0
    while processed < max_attempts:
        batch_done = 0
        while batch_done < batch_size and processed < max_attempts:
            publisher_data: tuple[tuple[_SnapshotRow, _SnapshotRow], ...] = ()
            with store.begin_read() as conn:
                job = _job_row(conn, job_id)
                if str(job["status"]) == "completed":
                    return
                item = conn.execute(
                    """
                    SELECT attempt_id, ordinal, revision FROM scheduler_usage_backfill_items
                    WHERE job_id = ? AND ordinal = ? AND status = 'pending'
                    """,
                    (job_id, int(job["high_water"])),
                ).fetchone()
                if item is None:
                    snapshot_count = int(job["snapshot_count"])
                    attempt_id = ""
                    ordinal = 0
                    stored_revision = ""
                    attempt_data = None
                    effect_data = None
                    finished = True
                else:
                    finished = False
                    snapshot_count = int(job["snapshot_count"])
                    attempt_id = str(item["attempt_id"])
                    ordinal = int(item["ordinal"])
                    stored_revision = str(item["revision"])
                    attempt_row = store.get_attempt_by_id(conn, attempt_id)
                    effect_row = None
                    if attempt_row is not None:
                        effect_row = store.get_effect_by_dispatch_id(
                            conn, str(attempt_row["dispatch_id"])
                        )
                    attempt_data = None if attempt_row is None else dict(attempt_row)
                    effect_data = None if effect_row is None else dict(effect_row)
                    publisher_data = tuple(
                        (_SnapshotRow(dict(publisher)), _SnapshotRow(dict(publisher_effect)))
                        for publisher, publisher_effect in _terminal_reconcile_publishers(
                            store, conn, attempt_id
                        )
                    )
            if finished:
                with store.begin_immediate() as conn:
                    _finish_job(conn, job_id, now_text=encode_utc_instant(now), completed=True)
                return
            if attempt_data is None or effect_data is None:
                _defer_missing_attempt(store, job_id, item_attempt=attempt_id, now=now)
                return
            if str(attempt_data["status"]) not in _TERMINAL:
                _mark_item(
                    store,
                    job_id,
                    attempt_id=attempt_id,
                    ordinal=ordinal,
                    status="skipped_unsettled",
                    diagnostics=("usage_attempt_active",),
                    now=now,
                    advance=True,
                    snapshot_count=snapshot_count,
                )
                processed += 1
                batch_done += 1
                continue
            planned = _classify(
                _SnapshotRow(attempt_data),
                _SnapshotRow(effect_data),
                artifacts,
                publishers=publisher_data,
            )
            if before_commit is not None:
                before_commit(attempt_id)
            outcome = _commit_candidate(
                store,
                planned,
                attempt_id=attempt_id,
                stored_revision=stored_revision,
                now=now,
            )
            if outcome == "revision_changed":
                _defer_revision(store, job_id, attempt_id=attempt_id, now=now)
                return
            if store.usage_backfill_barrier == "after_fact_before_cursor":
                raise UsageBackfillBarrier("after_fact_before_cursor")
            item_status: Literal["materialized", "retained", "conflict", "skipped_unsettled"]
            if outcome == "conflict":
                item_status = "conflict"
            elif outcome == "retained":
                item_status = "retained"
            elif outcome == "skipped_unsettled":
                item_status = "skipped_unsettled"
            else:
                item_status = "materialized"
            _mark_item(
                store,
                job_id,
                attempt_id=attempt_id,
                ordinal=ordinal,
                status=item_status,
                diagnostics=(),
                now=now,
                advance=True,
                snapshot_count=snapshot_count,
            )
            processed += 1
            batch_done += 1


def _commit_candidate(
    store: SqliteSchedulerStore,
    planned: _ReadyImport | _ClassifiedImport,
    *,
    attempt_id: str,
    stored_revision: str,
    now: datetime,
) -> Literal["written", "retained", "conflict", "skipped_unsettled", "revision_changed"]:
    with store.begin_immediate() as conn:
        attempt = store.get_attempt_by_id(conn, attempt_id)
        if attempt is None:
            return "revision_changed"
        effect = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
        publishers = _terminal_reconcile_publishers(store, conn, attempt_id)
        if effect is None or _item_revision(attempt, effect, publishers) != stored_revision:
            return "revision_changed"
        existing = store.get_usage_attempt_fact(conn, attempt_id)
        if (
            existing is not None
            and str(existing["settlement_status"]) == "pending"
            and not isinstance(planned, _ReadyImport)
        ):
            return "skipped_unsettled"
        if isinstance(planned, _ClassifiedImport):
            if existing is not None and str(existing["settlement_status"]) in {
                "settled",
                "conflict",
                "debt",
            }:
                return "retained"
            _write_classified_without_counters(
                store,
                conn,
                attempt=attempt,
                publisher_attempt_id=attempt_id,
                now=now,
                coverage=planned.coverage,
                settlement=planned.settlement,
                diagnostic=planned.diagnostic,
                import_origin="ingestion",
                configured_model=planned.configured_model,
                extra_diagnostics=planned.extra_diagnostics,
            )
        else:
            result = merge_usage_snapshot(
                store,
                conn,
                fact=planned.fact,
                observations=planned.observations,
                now=now,
            )
            store.acknowledge_classified_usage_facts(conn, now_text=encode_utc_instant(now))
            if result == "conflict":
                return "conflict"
        if not isinstance(planned, _ReadyImport):
            store.acknowledge_classified_usage_facts(conn, now_text=encode_utc_instant(now))
        return "written"


def _classify(
    attempt: _SnapshotRow,
    effect: _SnapshotRow,
    artifacts: ReadOnlyProtectedArtifactStore,
    publishers: tuple[tuple[_SnapshotRow, _SnapshotRow], ...] = (),
) -> _ReadyImport | _ClassifiedImport:
    effect_kind = str(effect["effect_kind"])
    attempt_id = str(attempt["attempt_id"])
    try:
        run_root = artifacts.run_root(str(attempt["run_id"]))
        authenticated = authenticate_process_attempt(run_root, attempt, effect_kind=effect_kind)  # type: ignore[arg-type]
    except (
        ProcessOutputIntegrityError,
        ProtectedArtifactError,
        ValueError,
        OSError,
        TypeError,
        KeyError,
    ) as exc:
        return _failure_classification(str(exc))
    outcome = authenticated.outcome or {}
    invocation = authenticated.invocation
    if (
        effect_kind == RUN_CURSOR_TURN_EFFECT_KIND
        and outcome.get("failure_kind") == "cursor_sdk_preflight_failed"
        and not outcome.get("native_run_id")
    ):
        return _ClassifiedImport(
            kind="malformed",
            coverage="not_applicable",
            settlement="settled",
            diagnostic=None,
            extra_diagnostics=(),
            configured_model=None,
        )
    projected = _attempt_from_authenticated(
        attempt,  # type: ignore[arg-type]
        invocation,
        outcome,
        effect_kind=effect_kind,
    )
    if projected is None:
        return _ClassifiedImport(
            kind="malformed",
            coverage="unknown",
            settlement="settled",
            diagnostic="usage_unattributed",
            extra_diagnostics=(),
            configured_model=None,
        )
    diagnostics = list(safe_diagnostic_codes(outcome.get("usage_diagnostics", [])))
    if any(code.startswith("conflicting_") for code in diagnostics):
        return _ClassifiedImport(
            kind="malformed",
            coverage="unknown",
            settlement="settled",
            diagnostic="usage_source_contradictory",
            extra_diagnostics=tuple(diagnostics),
            configured_model=projected.configured_model,
        )
    readonly = artifacts
    observations, incomplete = _load_published_observations(
        artifacts=readonly,
        publisher=projected,
        publisher_attempt_id=attempt_id,
        publisher_effect_kind=effect_kind,
        publisher_invocation=invocation,
        run_id=str(attempt["run_id"]),
        outcome=outcome,
        attempts={projected.attempt_id: projected},
        diagnostics=diagnostics,
    )
    raw_bindings = outcome.get("usage_observations", [])
    bound_count = (
        sum(1 for item in raw_bindings if isinstance(item, dict))
        if isinstance(raw_bindings, list)
        else 0
    )
    binding_gap = bool(incomplete) or bound_count > len(observations)
    historical: list[UsageObservationV1] = []
    historical_complete: bool | None = None
    if projected.provider == "codex" and (not observations or binding_gap):
        historical, historical_complete = _historical_codex_observations(
            artifacts=readonly,
            attempt=projected,
            diagnostics=diagnostics,
        )
    seen = {item.observation_id for item in observations}
    for item in historical:
        if item.observation_id not in seen:
            observations.append(item)
            seen.add(item.observation_id)
    if binding_gap and historical and len(historical) >= bound_count:
        binding_gap = False
        incomplete = set()
        diagnostics = [code for code in diagnostics if code != "usage_observation_integrity_error"]
    had_logical_observations = bool(observations)
    publisher_attempt_id, publisher_capture_complete = _fold_reconcile_publications(
        publishers=publishers,
        artifacts=readonly,
        projected=projected,
        observations=observations,
        diagnostics=diagnostics,
    )
    if publisher_capture_complete and not had_logical_observations:
        binding_gap = False
        incomplete = set()
    if not observations and diagnostics:
        kind = _source_kind(diagnostics, outcome, artifacts, str(attempt["run_id"]))
        return _ClassifiedImport(
            kind=kind,
            coverage=None if binding_gap else "unknown",
            settlement="debt" if binding_gap else "settled",
            diagnostic="usage_source_debt" if binding_gap else _diagnostic_for(kind),
            extra_diagnostics=tuple(dict.fromkeys(diagnostics)),
            configured_model=projected.configured_model,
        )
    if binding_gap and not observations:
        return _ClassifiedImport(
            kind=_source_kind(diagnostics, outcome, artifacts, str(attempt["run_id"])),
            coverage=None,
            settlement="debt",
            diagnostic="usage_source_debt",
            extra_diagnostics=tuple(dict.fromkeys(diagnostics)),
            configured_model=projected.configured_model,
        )
    binding_kind, binding_sha = _binding(attempt)  # type: ignore[arg-type]
    fence = _fence(attempt)  # type: ignore[arg-type]
    origin = "reconciliation" if publisher_attempt_id is not None else _origin(effect_kind)
    recorded_publisher = publisher_attempt_id or attempt_id
    writes: list[_ObservationWrite] = []
    for item in observations:
        try:
            writes.append(
                _observation_write(
                    item,
                    publisher_attempt_id=recorded_publisher,
                    import_origin=origin,
                    completion_fence_id=fence,
                    binding_kind=binding_kind,
                    binding_sha256=binding_sha,
                )
            )
        except ValueError:
            diagnostics.append("usage_observation_integrity_error")
    contribution_complete = _published_contribution_complete(outcome)
    if contribution_complete is None:
        contribution_complete = historical_complete
    if publisher_capture_complete and not had_logical_observations:
        contribution_complete = True
    if binding_gap:
        diagnostics.append("usage_source_debt")
    coverage = _fact_coverage(
        observations=writes,
        contribution_complete=bool(contribution_complete) and not incomplete,
        incomplete=bool(incomplete),
    )
    settlement: UsageSettlement = "debt" if binding_gap else "settled"
    payload = UsageFactPayloadV1(
        logical_attempt_id=attempt_id,
        run_id=str(attempt["run_id"]),
        provider=projected.provider,
        role=projected.role,
        stage=projected.stage,
        configured_model=projected.configured_model,
        terminal_status=_terminal_status(str(attempt["status"])),
        coverage=None if binding_gap else coverage,
        settlement_status=settlement,
        diagnostic_codes=tuple(dict.fromkeys(safe_diagnostic_codes(diagnostics))),
        import_origin=origin,
    )
    return _ReadyImport(
        fact=_FactWrite(
            payload=payload,
            source_paths=tuple(item.source_relative_path for item in writes),
            source_digests=tuple(item.source_digest for item in writes),
            publisher_attempt_id=recorded_publisher,
            source_dispatch_id=str(attempt["dispatch_id"]),
            completion_fence_id=fence,
            binding_kind=binding_kind,
            binding_sha256=binding_sha,
            import_origin=origin,
        ),
        observations=tuple(writes),
    )


def _fold_reconcile_publications(
    *,
    publishers: tuple[tuple[_SnapshotRow, _SnapshotRow], ...],
    artifacts: ReadOnlyProtectedArtifactStore,
    projected: _Attempt,
    observations: list[UsageObservationV1],
    diagnostics: list[str],
) -> tuple[str | None, bool]:
    """Import terminal reconciliation publications onto one logical attempt."""

    seen = {item.observation_id for item in observations}
    seen_keys = {item.native_key for item in observations}
    adopted_publisher: str | None = None
    capture_complete = False
    for publisher_attempt, publisher_effect in publishers:
        publisher_id = str(publisher_attempt["attempt_id"])
        effect_kind = str(publisher_effect["effect_kind"])
        try:
            authenticated = authenticate_process_attempt(
                artifacts.run_root(str(publisher_attempt["run_id"])),
                publisher_attempt,  # type: ignore[arg-type]
                effect_kind=effect_kind,
            )
        except (
            ProcessOutputIntegrityError,
            ProtectedArtifactError,
            ValueError,
            OSError,
            TypeError,
            KeyError,
        ):
            diagnostics.append("usage_source_missing")
            continue
        outcome = authenticated.outcome or {}
        loaded, publisher_incomplete = _load_published_observations(
            artifacts=artifacts,
            publisher=projected,
            publisher_attempt_id=publisher_id,
            publisher_effect_kind=effect_kind,
            publisher_invocation=authenticated.invocation,
            run_id=str(publisher_attempt["run_id"]),
            outcome=outcome,
            attempts={projected.attempt_id: projected},
            diagnostics=diagnostics,
        )
        added = False
        for item in loaded:
            if item.observation_id in seen or item.native_key in seen_keys:
                continue
            observations.append(item)
            seen.add(item.observation_id)
            seen_keys.add(item.native_key)
            added = True
        if not added:
            continue
        adopted_publisher = publisher_id
        if _published_contribution_complete(outcome) and not publisher_incomplete:
            capture_complete = True
    return adopted_publisher, capture_complete


def _failure_classification(message: str) -> _ClassifiedImport:
    lowered = message.lower()
    if "missing" in lowered or "not found" in lowered:
        kind: Literal["missing", "corrupt", "malformed"] = "missing"
    elif "invalid" in lowered or "validation" in lowered or "malformed" in lowered:
        kind = "malformed"
    else:
        kind = "corrupt"
    return _ClassifiedImport(
        kind=kind,
        coverage="unknown",
        settlement="settled",
        diagnostic=_diagnostic_for(kind),
        extra_diagnostics=(),
        configured_model=None,
    )


def _diagnostic_for(kind: Literal["missing", "corrupt", "malformed"]) -> str:
    if kind == "missing":
        return "usage_source_missing"
    if kind == "corrupt":
        return "usage_source_corrupt"
    return "usage_source_malformed"


def _source_kind(
    diagnostics: list[str],
    outcome: Mapping[str, object],
    artifacts: ReadOnlyProtectedArtifactStore,
    run_id: str,
) -> Literal["missing", "corrupt", "malformed"]:
    if any(code.startswith("invalid_") or code.startswith("malformed_") for code in diagnostics):
        return "malformed"
    raw = outcome.get("usage_observations", [])
    paths: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                paths.append(str(item["path"]))
    if paths:
        try:
            root = artifacts.run_root(run_id)
        except (ValueError, OSError):
            return "missing"
        if all(not (root / path).is_file() for path in paths):
            return "missing"
        return "corrupt"
    if any(code == "usage_observation_integrity_error" for code in diagnostics):
        return "corrupt"
    return "missing"


def _eligible_rows(conn: sqlite3.Connection, request: UsageBackfillRequest) -> list[sqlite3.Row]:
    clauses = [
        f"effect.effect_kind IN ({', '.join('?' for _ in _EFFECT_SQL)})",
        f"attempt.status IN ({', '.join('?' for _ in _TERMINAL)})",
    ]
    params: list[object] = [*_EFFECT_SQL, *sorted(_TERMINAL)]
    if request.scope_kind == "run":
        clauses.append("attempt.run_id = ?")
        params.append(request.scope_value)
    elif request.scope_kind == "project":
        clauses.append("json_extract(run.state_payload, '$.context.project_name') = ?")
        params.append(request.scope_value)
    rows = conn.execute(
        f"""
        SELECT attempt.attempt_id, attempt.run_id, attempt.status, attempt.updated_at,
               attempt.completion_fence_id, attempt.completion_envelope_sha256,
               attempt.dispatch_id, effect.effect_payload_sha256
        FROM scheduler_attempts AS attempt
        JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
        JOIN scheduler_runs AS run ON run.run_id = attempt.run_id
        WHERE {" AND ".join(clauses)}
        ORDER BY attempt.created_at, attempt.attempt_id
        """,  # noqa: S608
        tuple(params),
    ).fetchall()
    return [cast(sqlite3.Row, row) for row in rows]


def _terminal_reconcile_publishers(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    source_attempt_id: str,
) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    if not store.schema_supports_cursor_sdk_reconciliation(conn):
        return []
    reconciliation = store.get_cursor_sdk_reconciliation_by_source_attempt(
        conn, source_attempt_id=source_attempt_id
    )
    if reconciliation is None:
        return []
    rows = conn.execute(
        """
        SELECT attempt.attempt_id
        FROM scheduler_attempts AS attempt
        JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
        WHERE effect.effect_kind = ?
          AND json_extract(effect.effect_payload, '$.recovery_key') = ?
          AND attempt.status IN ('completed', 'failed', 'cancelled', 'uncertain')
        ORDER BY attempt.created_at, attempt.attempt_id
        """,
        (RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND, str(reconciliation["recovery_key"])),
    ).fetchall()
    publishers: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    for row in rows:
        publisher = store.get_attempt_by_id(conn, str(row["attempt_id"]))
        if publisher is None:
            continue
        publisher_effect = store.get_effect_by_dispatch_id(conn, str(publisher["dispatch_id"]))
        if publisher_effect is None:
            continue
        publishers.append((publisher, publisher_effect))
    return publishers


def _item_revision(
    attempt: sqlite3.Row | _SnapshotRow | Mapping[str, object],
    effect: object = None,
    publishers: Sequence[tuple[sqlite3.Row, sqlite3.Row]] = (),
) -> str:
    parts = [_revision(attempt, effect)]
    for publisher_attempt, publisher_effect in publishers:
        parts.append(_revision(publisher_attempt, publisher_effect))
    text = "~".join(parts)
    if len(text) <= 512:
        return text
    return canonical_sha256({"revision": text})


def _revision(
    attempt: sqlite3.Row | _SnapshotRow | Mapping[str, object], effect: object = None
) -> str:
    if effect is None and not isinstance(attempt, Mapping):
        effect_sha = attempt["effect_payload_sha256"]
    elif isinstance(effect, Mapping) or hasattr(effect, "__getitem__"):
        effect_sha = effect["effect_payload_sha256"]
    else:
        effect_sha = ""
    text = "|".join(
        [
            str(attempt["status"]),
            str(attempt["updated_at"]),
            str(attempt["completion_fence_id"] or ""),
            str(attempt["completion_envelope_sha256"] or ""),
            str(effect_sha or ""),
        ]
    )
    return text[:512]


def _job_row(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM scheduler_usage_backfill_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.NOT_FOUND, "usage backfill job not found"
        )
    return cast(sqlite3.Row, row)


def _finish_job(conn: sqlite3.Connection, job_id: str, *, now_text: str, completed: bool) -> None:
    conn.execute(
        """
        UPDATE scheduler_usage_backfill_jobs
        SET status = ?, updated_at = ?
        WHERE job_id = ?
        """,
        ("completed" if completed else "stopped", now_text, job_id),
    )


def _mark_item(
    store: SqliteSchedulerStore,
    job_id: str,
    *,
    attempt_id: str,
    ordinal: int,
    status: str,
    diagnostics: tuple[str, ...],
    now: datetime,
    advance: bool,
    snapshot_count: int,
) -> None:
    now_text = encode_utc_instant(now)
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_usage_backfill_items
            SET status = ?, diagnostic_codes = ?
            WHERE job_id = ? AND attempt_id = ?
            """,
            (
                status,
                canonical_json(list(safe_diagnostic_codes(list(diagnostics)))),
                job_id,
                attempt_id,
            ),
        )
        if not advance:
            return
        completed = ordinal + 1 >= snapshot_count
        conn.execute(
            """
            UPDATE scheduler_usage_backfill_jobs
            SET high_water = ?, status = ?, updated_at = ?
            WHERE job_id = ? AND high_water = ?
            """,
            (
                ordinal + 1,
                "completed" if completed else "running",
                now_text,
                job_id,
                ordinal,
            ),
        )


def _defer_revision(
    store: SqliteSchedulerStore, job_id: str, *, attempt_id: str, now: datetime
) -> None:
    now_text = encode_utc_instant(now)
    with store.begin_immediate() as conn:
        attempt = store.get_attempt_by_id(conn, attempt_id)
        effect = None
        if attempt is not None:
            effect = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
        if attempt is not None and effect is not None:
            conn.execute(
                """
                UPDATE scheduler_usage_backfill_items
                SET revision = ?, status = 'pending', diagnostic_codes = ?
                WHERE job_id = ? AND attempt_id = ?
                """,
                (
                    _item_revision(
                        attempt,
                        effect,
                        _terminal_reconcile_publishers(store, conn, attempt_id),
                    ),
                    canonical_json(["attempt_revision_changed"]),
                    job_id,
                    attempt_id,
                ),
            )
        conn.execute(
            """
            UPDATE scheduler_usage_backfill_jobs
            SET status = 'stopped', updated_at = ? WHERE job_id = ?
            """,
            (now_text, job_id),
        )


def _defer_missing_attempt(
    store: SqliteSchedulerStore, job_id: str, *, item_attempt: str, now: datetime
) -> None:
    _mark_item(
        store,
        job_id,
        attempt_id=item_attempt,
        ordinal=0,
        status="deferred",
        diagnostics=("usage_source_missing",),
        now=now,
        advance=False,
        snapshot_count=0,
    )
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_usage_backfill_jobs
            SET status = 'stopped', updated_at = ? WHERE job_id = ?
            """,
            (encode_utc_instant(now), job_id),
        )


def _readiness(
    store: SqliteSchedulerStore,
    artifacts: ReadOnlyProtectedArtifactStore,
    request: UsageBackfillRequest,
    *,
    job_id: str | None,
    job_status: Literal["not_started", "running", "completed", "stopped"],
) -> UsageBackfillReport:
    inspect_targets: list[
        tuple[
            dict[str, object],
            dict[str, object],
            tuple[tuple[dict[str, object], dict[str, object]], ...],
        ]
    ] = []
    with store.begin_read() as conn:
        supports = store.schema_supports_usage_statistics(conn)
        active = _count_active(conn, request)
        eligible = _eligible_rows(conn, request)
        known = partial = unknown = unattributed = 0
        materialized = conflicting = lost = 0
        unmaterialized = 0
        pending_facts = 0
        debt_facts = 0
        diagnostics: list[str] = []
        inspected = 0
        for row in eligible:
            attempt_id = str(row["attempt_id"])
            fact = store.get_usage_attempt_fact(conn, attempt_id) if supports else None
            if fact is None:
                unmaterialized += 1
                if inspected < request.max_attempts:
                    inspected += 1
                    attempt = store.get_attempt_by_id(conn, attempt_id)
                    effect = None
                    if attempt is not None:
                        effect = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
                    if attempt is not None and effect is not None:
                        publishers = tuple(
                            (dict(publisher), dict(publisher_effect))
                            for publisher, publisher_effect in _terminal_reconcile_publishers(
                                store, conn, attempt_id
                            )
                        )
                        inspect_targets.append((dict(attempt), dict(effect), publishers))
                continue
            payload = _validated_fact(fact)
            if payload.settlement_status == "pending":
                pending_facts += 1
                diagnostics.append("usage_statistics_pending")
                continue
            if payload.coverage == "not_applicable":
                materialized += 1
                continue
            loaded = _validated_observations(
                conn,
                attempt_id,
                payload.observation_ids,
                run_id=str(fact["run_id"]),
                publisher_attempt_id=str(fact["publisher_attempt_id"]),
                provider=payload.provider,
                role=payload.role,
                stage=payload.stage,
            )
            if payload.coverage == "complete" and not loaded:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CORRUPTION,
                    "usage statistics payload is invalid",
                )
            materialized += 1
            codes = set(payload.diagnostic_codes)
            if payload.settlement_status == "debt":
                debt_facts += 1
                diagnostics.append("usage_source_debt")
                if codes & _LOSS_CODES:
                    lost += 1
                diagnostics.extend(payload.diagnostic_codes)
                continue
            if payload.settlement_status == "conflict":
                conflicting += 1
            if codes & _LOSS_CODES:
                lost += 1
            if "usage_unattributed" in codes:
                unattributed += 1
            if payload.coverage == "complete":
                known += 1
            elif payload.coverage == "partial":
                partial += 1
            else:
                unknown += 1
            diagnostics.extend(payload.diagnostic_codes)
        eligible_count = len(eligible)
    missing = corrupt = malformed = 0
    for attempt_data, effect_data, publisher_data in inspect_targets:
        planned = _classify(
            _SnapshotRow(attempt_data),
            _SnapshotRow(effect_data),
            artifacts,
            publishers=tuple(
                (_SnapshotRow(publisher), _SnapshotRow(publisher_effect))
                for publisher, publisher_effect in publisher_data
            ),
        )
        if not isinstance(planned, _ClassifiedImport):
            continue
        if planned.kind == "missing":
            missing += 1
        elif planned.kind == "corrupt":
            corrupt += 1
        elif planned.coverage != "not_applicable":
            malformed += 1
        if planned.diagnostic:
            diagnostics.append(planned.diagnostic)
    if unmaterialized:
        diagnostics.append("backfill_required")
    if lost:
        diagnostics.append("usage_source_loss")
    if inspected < unmaterialized:
        diagnostics.append("backfill_inspection_bounded")
    consumption = known + partial + unknown + unattributed + unmaterialized
    provider = _provider_coverage(consumption, known, partial)
    unsettled = active + pending_facts + debt_facts
    remaining = unmaterialized + unsettled + conflicting
    materialization: Literal["complete", "incomplete"] = (
        "complete" if unmaterialized == 0 and pending_facts == 0 else "incomplete"
    )
    return UsageBackfillReport(
        mode=request.mode,
        scope_kind=request.scope_kind,
        scope_value=request.scope_value,
        job_id=job_id,
        job_status=job_status,
        eligible_terminal=eligible_count,
        materialized=materialized,
        known=known,
        partial=partial,
        unknown=unknown,
        unattributed=unattributed,
        unmaterialized=unmaterialized,
        active_unsettled=unsettled,
        missing_sources=missing,
        corrupt_sources=corrupt,
        malformed_sources=malformed,
        conflicting=conflicting,
        lost_source=lost,
        provider_coverage=provider,
        materialization=materialization,
        statistically_ready=remaining == 0,
        remaining=remaining,
        diagnostics=tuple(sorted(set(safe_diagnostic_codes(diagnostics)))),
    )


def _validated_fact(row: sqlite3.Row) -> UsageFactPayloadV1:
    try:
        payload = UsageFactPayloadV1.model_validate_json(str(row["fact_payload"]))
        if canonical_sha256(payload.model_dump(mode="json")) != str(row["payload_sha256"]):
            raise ValueError("usage fact digest mismatch")
    except (ValidationError, ValueError, json.JSONDecodeError, TypeError) as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "usage statistics payload is invalid",
        ) from exc
    return payload


def _count_active(conn: sqlite3.Connection, request: UsageBackfillRequest) -> int:
    clauses = [
        f"effect.effect_kind IN ({', '.join('?' for _ in _EFFECT_SQL)})",
        "attempt.status IN ('launching', 'active')",
    ]
    params: list[object] = [*_EFFECT_SQL]
    if request.scope_kind == "run":
        clauses.append("attempt.run_id = ?")
        params.append(request.scope_value)
    elif request.scope_kind == "project":
        clauses.append("json_extract(run.state_payload, '$.context.project_name') = ?")
        params.append(request.scope_value)
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM scheduler_attempts AS attempt
        JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
        JOIN scheduler_runs AS run ON run.run_id = attempt.run_id
        WHERE {" AND ".join(clauses)}
        """,  # noqa: S608
        tuple(params),
    ).fetchone()
    return int(row[0]) if row is not None else 0
