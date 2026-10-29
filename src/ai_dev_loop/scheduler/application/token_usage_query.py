"""Read-only, evidence-authenticated token-usage aggregation."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from ai_dev_loop.integration_api.process_output_auth import (
    ProcessOutputIntegrityError,
    authenticate_process_attempt,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.token_usage_capture import MAX_USAGE_OBSERVATION_BYTES
from ai_dev_loop.scheduler.domain.codex_contract import (
    CODEX_ATTEMPT_EFFECT_KINDS,
    IMPLEMENTER_ATTEMPT_EFFECT_KINDS,
    MAX_CODEX_EVENTS_ARTIFACT_BYTES,
)
from ai_dev_loop.scheduler.domain.cursor_contract import (
    MAX_CURSOR_EVENTS_ARTIFACT_BYTES,
    RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.token_usage import (
    NormalizedUsage,
    ProviderNativeUsage,
    RunUsageReport,
    SequenceUsageReport,
    UsageBreakdown,
    UsageCategoryAggregate,
    UsageCoverage,
    UsageObservationV1,
    UsageScopeReport,
    normalize_codex_usage,
    usage_observation_id,
)
from ai_dev_loop.scheduler.infrastructure.paths import (
    default_artifact_root,
    default_engine_db_path,
)
from ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts import (
    ReadOnlyProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_INFERENCE_EFFECTS = frozenset(
    {RUN_CURSOR_TURN_EFFECT_KIND} | CODEX_ATTEMPT_EFFECT_KINDS | IMPLEMENTER_ATTEMPT_EFFECT_KINDS
)
_SOURCE_ARTIFACT_READ_LIMIT = max(MAX_CURSOR_EVENTS_ARTIFACT_BYTES, MAX_CODEX_EVENTS_ARTIFACT_BYTES)
_CONFIGURED_MODEL_MAX_CODE_POINTS = 256


class TokenUsageScopeLimit(Exception):
    """Direct usage collection exceeded its attempt or deadline boundary."""


@dataclass(frozen=True)
class DirectUsageAttempt:
    run_id: str
    attempt_id: str
    provider: Literal["cursor", "codex"]
    role: Literal["implementer", "reviewer"]
    stage: Literal["initial", "correction", "review"]
    configured_model: str | None
    observations: tuple[UsageObservationV1, ...]
    state: Literal["complete", "partial", "unknown"]


@dataclass(frozen=True)
class DirectUsageEvidence:
    attempts: tuple[DirectUsageAttempt, ...]
    unattributed_attempts: int
    diagnostics: tuple[str, ...]


def configured_model_from_invocation(
    provider: Literal["cursor", "codex"],
    role: Literal["implementer", "reviewer"],
    invocation: dict[str, object],
) -> str | None:
    """Return the invocation's configured model, or None when it was not recorded."""

    if provider == "cursor":
        raw = invocation.get("cursor_model")
    elif role == "implementer":
        raw = invocation.get("model")
    else:
        raw = invocation.get("review_model")
    if not isinstance(raw, str) or isinstance(raw, bool):
        return None
    if any(unicodedata.category(character) == "Cc" for character in raw):
        return None
    text = raw.strip()
    if not text or len(text) > _CONFIGURED_MODEL_MAX_CODE_POINTS:
        return None
    return text


def _checkpoint(checkpoint: Callable[[], None] | None) -> None:
    if checkpoint is not None:
        checkpoint()


_NORMALIZED_CATEGORIES = tuple(NormalizedUsage.model_fields)
_NATIVE_CATEGORIES = {
    "cursor": (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
        "reasoning_tokens",
    ),
    "codex": (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ),
}


@dataclass(frozen=True)
class _Attempt:
    run_id: str
    attempt_id: str
    dispatch_id: str
    provider: Literal["cursor", "codex"]
    role: Literal["implementer", "reviewer"]
    stage: Literal["initial", "correction", "review"]
    configured_model: str | None
    outcome: dict[str, object] | None


def _rows(conn: sqlite3.Connection, run_id: str) -> tuple[sqlite3.Row, ...]:
    rows = conn.execute(
        """
        SELECT attempt.*, effect.effect_kind
        FROM scheduler_attempts AS attempt
        JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
        WHERE attempt.run_id = ?
        ORDER BY attempt.created_at, attempt.attempt_id
        """,
        (run_id,),
    ).fetchall()
    return tuple(cast(sqlite3.Row, row) for row in rows)


def _attempt_from_authenticated(
    row: sqlite3.Row,
    invocation: dict[str, object],
    outcome: dict[str, object] | None,
    *,
    effect_kind: str | None = None,
) -> _Attempt | None:
    effect = effect_kind if effect_kind is not None else str(row["effect_kind"])
    if effect not in _INFERENCE_EFFECTS:
        return None
    if (
        effect == RUN_CURSOR_TURN_EFFECT_KIND
        and outcome is not None
        and outcome.get("failure_kind") == "cursor_sdk_preflight_failed"
        and not outcome.get("native_run_id")
    ):
        return None
    attempt_id = str(row["attempt_id"])
    run_id = str(row["run_id"])
    dispatch_id = str(row["dispatch_id"])
    if effect == RUN_CURSOR_TURN_EFFECT_KIND:
        raw_iteration = invocation.get("iteration")
        if isinstance(raw_iteration, bool) or not isinstance(raw_iteration, int):
            raise ProcessOutputIntegrityError("Cursor usage invocation iteration invalid")
        return _Attempt(
            run_id=run_id,
            attempt_id=attempt_id,
            dispatch_id=dispatch_id,
            provider="cursor",
            role="implementer",
            stage="initial" if raw_iteration == 1 else "correction",
            configured_model=configured_model_from_invocation("cursor", "implementer", invocation),
            outcome=outcome,
        )
    if effect in IMPLEMENTER_ATTEMPT_EFFECT_KINDS:
        turn = invocation.get("turn")
        if turn not in {"initial", "correction"}:
            raise ProcessOutputIntegrityError("Codex implementer turn invalid")
        return _Attempt(
            run_id=run_id,
            attempt_id=attempt_id,
            dispatch_id=dispatch_id,
            provider="codex",
            role="implementer",
            stage=cast(Literal["initial", "correction"], turn),
            configured_model=configured_model_from_invocation("codex", "implementer", invocation),
            outcome=outcome,
        )
    return _Attempt(
        run_id=run_id,
        attempt_id=attempt_id,
        dispatch_id=dispatch_id,
        provider="codex",
        role="reviewer",
        stage="review",
        configured_model=configured_model_from_invocation("codex", "reviewer", invocation),
        outcome=outcome,
    )


def _load_published_observations(
    *,
    artifacts: ReadOnlyProtectedArtifactStore,
    publisher: _Attempt | None,
    publisher_attempt_id: str,
    publisher_effect_kind: str,
    publisher_invocation: dict[str, object],
    run_id: str,
    outcome: dict[str, object],
    attempts: dict[str, _Attempt],
    diagnostics: list[str],
    checkpoint: Callable[[], None] | None = None,
) -> tuple[list[UsageObservationV1], set[str]]:
    observations: list[UsageObservationV1] = []
    logical_attempt_id: str | None = publisher.attempt_id if publisher is not None else None
    if logical_attempt_id is None and publisher_effect_kind == RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND:
        source_attempt_id = publisher_invocation.get("source_attempt_id")
        if isinstance(source_attempt_id, str) and source_attempt_id in attempts:
            logical_attempt_id = source_attempt_id
    incomplete_attempts: set[str] = set()
    raw_bindings = outcome.get("usage_observations", [])
    if not isinstance(raw_bindings, list):
        diagnostics.append("invalid_usage_observation_bindings")
        if logical_attempt_id is not None:
            incomplete_attempts.add(logical_attempt_id)
        return observations, incomplete_attempts
    for raw in raw_bindings:
        if not isinstance(raw, dict):
            diagnostics.append("invalid_usage_observation_binding")
            if logical_attempt_id is not None:
                incomplete_attempts.add(logical_attempt_id)
            continue
        path = raw.get("path")
        digest = raw.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            diagnostics.append("invalid_usage_observation_binding")
            if logical_attempt_id is not None:
                incomplete_attempts.add(logical_attempt_id)
            continue
        _checkpoint(checkpoint)
        try:
            data = artifacts.read_verified_bytes(
                run_id,
                path,
                expected_sha256=digest,
                max_bytes=MAX_USAGE_OBSERVATION_BYTES,
            )
            observation = UsageObservationV1.model_validate_json(data)
            logical = attempts.get(observation.scheduler_attempt_id)
            if logical is None:
                raise ValueError("usage observation has no authenticated inference attempt")
            if observation.publisher_attempt_id != publisher_attempt_id and not (
                publisher_effect_kind == RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
                and publisher_invocation.get("source_attempt_id")
                == observation.scheduler_attempt_id
            ):
                raise ValueError("usage observation publisher mismatch")
            if observation.run_id != run_id:
                raise ValueError("usage observation run mismatch")
            if (
                observation.provider != logical.provider
                or observation.role != logical.role
                or observation.stage != logical.stage
                or observation.source_dispatch_id != logical.dispatch_id
            ):
                raise ValueError("usage observation attribution mismatch")
        except Exception:
            diagnostics.append("usage_observation_integrity_error")
            if logical_attempt_id is not None:
                incomplete_attempts.add(logical_attempt_id)
            continue
        _checkpoint(checkpoint)
        try:
            artifacts.read_verified_bytes(
                run_id,
                observation.source_artifact_path,
                expected_sha256=observation.source_artifact_sha256,
                max_bytes=_SOURCE_ARTIFACT_READ_LIMIT,
            )
        except Exception:
            diagnostics.append("usage_observation_integrity_error")
            if logical_attempt_id is not None:
                incomplete_attempts.add(logical_attempt_id)
            continue
        observations.append(observation)
    return observations, incomplete_attempts


def _historical_codex_observations(
    *,
    artifacts: ReadOnlyProtectedArtifactStore,
    attempt: _Attempt,
    diagnostics: list[str],
    checkpoint: Callable[[], None] | None = None,
) -> tuple[list[UsageObservationV1], bool | None]:
    outcome = attempt.outcome
    if outcome is None:
        return [], None
    path = outcome.get("events_path")
    digest = outcome.get("events_sha256")
    if not isinstance(path, str) or not isinstance(digest, str) or not digest:
        return [], None
    _checkpoint(checkpoint)
    try:
        data = artifacts.read_verified_bytes(
            attempt.run_id,
            path,
            expected_sha256=digest,
            max_bytes=_SOURCE_ARTIFACT_READ_LIMIT,
        )
        text = data.decode("utf-8")
    except Exception:
        diagnostics.append("historical_codex_usage_source_invalid")
        return [], False
    parsed: list[tuple[int, ProviderNativeUsage, NormalizedUsage]] = []
    ordinal = 0
    stream_complete = not bool(
        outcome.get("stdout_truncated")
        or outcome.get("review_output_truncated")
        or outcome.get("output_incomplete")
    )
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        ordinal += 1
        if event.get("usage") is None:
            diagnostics.append(f"missing_historical_codex_usage_turn_{ordinal}")
            continue
        try:
            native, normalized = normalize_codex_usage(event["usage"])
            parsed.append((ordinal, native, normalized))
        except Exception:
            diagnostics.append(f"invalid_historical_codex_usage_turn_{ordinal}")
    contribution_complete = stream_complete and ordinal > 0 and ordinal == len(parsed)
    observations: list[UsageObservationV1] = []
    for event_ordinal, native, normalized in parsed:
        native_key = str(event_ordinal)
        observations.append(
            UsageObservationV1(
                observation_id=usage_observation_id(
                    provider="codex",
                    role=attempt.role,
                    scheduler_attempt_id=attempt.attempt_id,
                    native_key=native_key,
                ),
                run_id=attempt.run_id,
                provider="codex",
                role=attempt.role,
                stage=attempt.stage,
                scheduler_attempt_id=attempt.attempt_id,
                publisher_attempt_id=attempt.attempt_id,
                source_dispatch_id=attempt.dispatch_id,
                native_key=native_key,
                source_artifact_path=path,
                source_artifact_sha256=digest,
                source_format="codex-exec-jsonl-v1",
                source_event_ordinal=event_ordinal,
                capture_complete=contribution_complete,
                availability="known" if contribution_complete else "partial",
                native=native,
                normalized=normalized,
            )
        )
    return observations, contribution_complete


def _published_contribution_complete(outcome: dict[str, object] | None) -> bool | None:
    if outcome is None:
        return None
    capture = outcome.get("usage_capture")
    if not isinstance(capture, dict):
        return None
    complete = capture.get("contribution_complete")
    completed = capture.get("completed_contributions")
    observed = capture.get("observed_contributions")
    if (
        not isinstance(complete, bool)
        or isinstance(completed, bool)
        or not isinstance(completed, int)
        or completed < 0
        or isinstance(observed, bool)
        or not isinstance(observed, int)
        or observed < 0
        or observed > completed
    ):
        return False
    return complete


def _coverage(
    inference_attempts: int,
    known_attempts: int,
    partial_attempts: int,
) -> UsageCoverage:
    if inference_attempts == 0:
        return "not_applicable"
    if known_attempts == inference_attempts:
        return "complete"
    if known_attempts or partial_attempts:
        return "partial"
    return "unknown"


def _category(
    values: list[int | None],
    *,
    inference_attempts: int,
    complete_attempts: int,
) -> UsageCategoryAggregate:
    known = [value for value in values if value is not None]
    if inference_attempts == 0:
        coverage: UsageCoverage = "not_applicable"
    elif complete_attempts == inference_attempts:
        coverage = "complete"
    elif known:
        coverage = "partial"
    else:
        coverage = "unknown"
    return UsageCategoryAggregate(
        known_total=sum(known) if known else None,
        known_observations=len(known),
        observation_count=len(values),
        coverage=coverage,
    )


def _aggregate_scope(
    *,
    scope: Literal["direct", "recovery_lineage", "sequence"],
    run_ids: tuple[str, ...],
    attempts: dict[str, _Attempt],
    observations: dict[str, list[UsageObservationV1]],
    diagnostics: list[str],
    attempt_completeness: dict[str, bool] | None = None,
    unattributed_attempts: int = 0,
) -> UsageScopeReport:
    attempt_completeness = attempt_completeness or {}
    groups: dict[tuple[str, str, str], list[_Attempt]] = {}
    for attempt in attempts.values():
        groups.setdefault((attempt.provider, attempt.role, attempt.stage), []).append(attempt)
    breakdowns: list[UsageBreakdown] = []
    for (provider, role, stage), grouped in sorted(groups.items()):
        complete_count = 0
        partial_count = 0
        unknown_count = 0
        all_observations: list[UsageObservationV1] = []
        for attempt in grouped:
            items = observations.get(attempt.attempt_id, [])
            all_observations.extend(items)
            if not items:
                unknown_count += 1
            elif not attempt_completeness.get(
                attempt.attempt_id,
                all(item.availability == "known" for item in items),
            ):
                partial_count += 1
            else:
                complete_count += 1
        normalized_categories = {
            name: _category(
                [getattr(item.normalized, name) for item in all_observations],
                inference_attempts=len(grouped),
                complete_attempts=sum(
                    1
                    for attempt in grouped
                    if (items := observations.get(attempt.attempt_id, []))
                    and attempt_completeness.get(
                        attempt.attempt_id,
                        all(item.availability == "known" for item in items),
                    )
                    and all(
                        item.availability == "known" and getattr(item.normalized, name) is not None
                        for item in items
                    )
                ),
            )
            for name in _NORMALIZED_CATEGORIES
        }
        native_categories = {
            name: _category(
                [getattr(item.native, name) for item in all_observations],
                inference_attempts=len(grouped),
                complete_attempts=sum(
                    1
                    for attempt in grouped
                    if (items := observations.get(attempt.attempt_id, []))
                    and attempt_completeness.get(
                        attempt.attempt_id,
                        all(item.availability == "known" for item in items),
                    )
                    and all(
                        item.availability == "known" and getattr(item.native, name) is not None
                        for item in items
                    )
                ),
            )
            for name in _NATIVE_CATEGORIES[provider]
        }
        totals = [item.normalized.total_tokens for item in all_observations]
        breakdowns.append(
            UsageBreakdown(
                provider=cast(Literal["cursor", "codex"], provider),
                role=cast(Literal["implementer", "reviewer"], role),
                stage=cast(Literal["initial", "correction", "review"], stage),
                known_total_tokens=sum(totals) if totals else None,
                inference_attempts=len(grouped),
                known_attempts=complete_count,
                partial_attempts=partial_count,
                unknown_attempts=unknown_count,
                coverage=_coverage(len(grouped), complete_count, partial_count),
                categories=normalized_categories,
                native_categories=native_categories,
            )
        )
    inference_attempts = len(attempts) + unattributed_attempts
    known_attempts = sum(item.known_attempts for item in breakdowns)
    partial_attempts = sum(item.partial_attempts for item in breakdowns)
    unknown_attempts = sum(item.unknown_attempts for item in breakdowns) + unattributed_attempts
    totals = [
        observation.normalized.total_tokens
        for items in observations.values()
        for observation in items
    ]
    return UsageScopeReport(
        scope=scope,
        run_ids=run_ids,
        known_total_tokens=sum(totals) if totals else None,
        inference_attempts=inference_attempts,
        known_attempts=known_attempts,
        partial_attempts=partial_attempts,
        unknown_attempts=unknown_attempts,
        coverage=_coverage(inference_attempts, known_attempts, partial_attempts),
        breakdowns=tuple(breakdowns),
        diagnostics=tuple(sorted(set(diagnostics))),
    )


class TokenUsageReadService:
    """Projects usage without migrations, backfills, provider runtimes, or auth calls."""

    def __init__(
        self,
        store: SqliteSchedulerStore,
        *,
        artifact_root: Path | None = None,
    ) -> None:
        self.store = store
        self.artifacts = ReadOnlyProtectedArtifactStore(artifact_root or default_artifact_root())

    def _run_ids_with_ancestors(self, conn: sqlite3.Connection, run_id: str) -> tuple[str, ...]:
        seen: set[str] = set()
        pending = [run_id]
        relations = (
            ("scheduler_review_recovery_successors", None),
            ("scheduler_cursor_initial_recoveries", "ready"),
            ("scheduler_codex_implementer_recoveries", "ready"),
        )
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            for table, required_status in relations:
                if not self.store.schema_has_table(conn, table):
                    continue
                sql = f"SELECT source_run_id FROM {table} WHERE successor_run_id = ?"  # noqa: S608
                params: tuple[object, ...] = (current,)
                if required_status is not None:
                    sql += " AND status = ?"
                    params = (current, required_status)
                row = conn.execute(sql, params).fetchone()
                if row is not None:
                    pending.append(str(row["source_run_id"]))
        return tuple(sorted(seen))

    def _sequence_run_ids(self, conn: sqlite3.Connection, sequence_id: str) -> tuple[str, ...]:
        if not self.store.schema_has_table(conn, "scheduler_sequences"):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.SCHEMA, "sequence usage is unavailable"
            )
        exists = conn.execute(
            "SELECT 1 FROM scheduler_sequences WHERE sequence_id = ?", (sequence_id,)
        ).fetchone()
        if exists is None:
            raise SchedulerEngineError(SchedulerEngineErrorKind.NOT_FOUND, "sequence not found")
        if self.store.schema_has_table(conn, "scheduler_sequence_run_attempts"):
            rows = conn.execute(
                """
                SELECT DISTINCT run_id FROM scheduler_sequence_run_attempts
                WHERE sequence_id = ? ORDER BY run_id
                """,
                (sequence_id,),
            ).fetchall()
            return tuple(str(row["run_id"]) for row in rows)
        rows = conn.execute(
            """
            SELECT run_id FROM scheduler_runs
            WHERE json_extract(state_payload, '$.context.sequence.sequence_id') = ?
            ORDER BY run_id
            """,
            (sequence_id,),
        ).fetchall()
        return tuple(str(row["run_id"]) for row in rows)

    def _collect(
        self,
        conn: sqlite3.Connection,
        run_ids: tuple[str, ...],
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> tuple[
        dict[str, _Attempt],
        dict[str, list[UsageObservationV1]],
        dict[str, bool],
        int,
        list[str],
    ]:
        attempts: dict[str, _Attempt] = {}
        attempt_completeness: dict[str, bool] = {}
        unattributed_attempts = 0
        authenticated_rows: list[
            tuple[sqlite3.Row, dict[str, object], dict[str, object] | None]
        ] = []
        diagnostics: list[str] = []
        for run_id in run_ids:
            rows = _rows(conn, run_id)
            try:
                run_root = self.artifacts.run_root(run_id)
            except ValueError as exc:
                message = str(exc)
                if message in {
                    "artifact root missing",
                    "runs artifact root missing",
                    "run artifact root missing",
                }:
                    diagnostics.append("usage_evidence_not_recorded")
                else:
                    diagnostics.append("usage_evidence_integrity_error")
                unattributed_attempts += sum(
                    1 for row in rows if str(row["effect_kind"]) in _INFERENCE_EFFECTS
                )
                continue
            for row in rows:
                effect = str(row["effect_kind"])
                if (
                    effect not in _INFERENCE_EFFECTS
                    and effect != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
                ):
                    continue
                _checkpoint(checkpoint)
                try:
                    authenticated = authenticate_process_attempt(run_root, row, effect_kind=effect)
                    authenticated_rows.append(
                        (row, authenticated.invocation, authenticated.outcome)
                    )
                    if authenticated.outcome is not None:
                        raw_diagnostics = authenticated.outcome.get("usage_diagnostics", [])
                        if isinstance(raw_diagnostics, list):
                            diagnostics.extend(
                                item
                                for item in raw_diagnostics
                                if isinstance(item, str)
                                and 0 < len(item) <= 80
                                and item.replace("_", "").isalnum()
                            )
                    attempt = _attempt_from_authenticated(
                        row, authenticated.invocation, authenticated.outcome
                    )
                    if attempt is not None:
                        attempts[attempt.attempt_id] = attempt
                        complete = _published_contribution_complete(authenticated.outcome)
                        if complete is not None:
                            attempt_completeness[attempt.attempt_id] = complete
                except (ProcessOutputIntegrityError, ValueError):
                    diagnostics.append("attempt_usage_evidence_invalid")
                    if effect in _INFERENCE_EFFECTS:
                        unattributed_attempts += 1
        observations: dict[str, list[UsageObservationV1]] = {}
        seen: set[str] = set()
        publication_incomplete: set[str] = set()
        for row, invocation, outcome in authenticated_rows:
            if outcome is None:
                continue
            publisher_id = str(row["attempt_id"])
            publisher = attempts.get(publisher_id)
            loaded, incomplete = _load_published_observations(
                artifacts=self.artifacts,
                publisher=publisher,
                publisher_attempt_id=publisher_id,
                publisher_effect_kind=str(row["effect_kind"]),
                publisher_invocation=invocation,
                run_id=str(row["run_id"]),
                outcome=outcome,
                attempts=attempts,
                diagnostics=diagnostics,
                checkpoint=checkpoint,
            )
            publication_incomplete.update(incomplete)
            for attempt_id in incomplete:
                attempt_completeness[attempt_id] = False
            for observation in loaded:
                if observation.observation_id in seen:
                    continue
                seen.add(observation.observation_id)
                observations.setdefault(observation.scheduler_attempt_id, []).append(observation)
        for attempt in attempts.values():
            if attempt.provider != "codex":
                continue
            historical, complete = _historical_codex_observations(
                artifacts=self.artifacts,
                attempt=attempt,
                diagnostics=diagnostics,
                checkpoint=checkpoint,
            )
            if complete is not None:
                attempt_completeness[attempt.attempt_id] = (
                    complete
                    and attempt_completeness.get(attempt.attempt_id) is not False
                    and attempt.attempt_id not in publication_incomplete
                )
            for observation in historical:
                if observation.observation_id in seen:
                    continue
                seen.add(observation.observation_id)
                observations.setdefault(attempt.attempt_id, []).append(observation)
        return (
            attempts,
            observations,
            attempt_completeness,
            unattributed_attempts,
            diagnostics,
        )

    def collect_direct(
        self,
        run_ids: tuple[str, ...],
        *,
        max_attempts: int,
        checkpoint: Callable[[], None] | None = None,
    ) -> DirectUsageEvidence:
        """Authenticate direct usage after bounding inference attempts.

        Artifact reads start only once the attempt count is known to fit.
        """

        if max_attempts < 0:
            raise TokenUsageScopeLimit
        unique_ids = tuple(dict.fromkeys(run_ids))
        with self.store.begin_read() as conn:
            counted = 0
            for run_id in unique_ids:
                _checkpoint(checkpoint)
                for row in _rows(conn, run_id):
                    if str(row["effect_kind"]) in _INFERENCE_EFFECTS:
                        counted += 1
                        if counted > max_attempts:
                            raise TokenUsageScopeLimit
            attempts, observations, completeness, unattributed, diagnostics = self._collect(
                conn,
                unique_ids,
                checkpoint=checkpoint,
            )
        evidence_attempts: list[DirectUsageAttempt] = []
        for attempt in attempts.values():
            items = tuple(observations.get(attempt.attempt_id, ()))
            if not items:
                state: Literal["complete", "partial", "unknown"] = "unknown"
            elif not completeness.get(
                attempt.attempt_id,
                all(item.availability == "known" for item in items),
            ):
                state = "partial"
            else:
                state = "complete"
            evidence_attempts.append(
                DirectUsageAttempt(
                    run_id=attempt.run_id,
                    attempt_id=attempt.attempt_id,
                    provider=attempt.provider,
                    role=attempt.role,
                    stage=attempt.stage,
                    configured_model=attempt.configured_model,
                    observations=items,
                    state=state,
                )
            )
        return DirectUsageEvidence(
            attempts=tuple(evidence_attempts),
            unattributed_attempts=unattributed,
            diagnostics=tuple(sorted(set(diagnostics))),
        )

    def run_report(self, run_id: str) -> RunUsageReport:
        with self.store.begin_read() as conn:
            self.store.get_run_row(conn, run_id)
            lineage_ids = self._run_ids_with_ancestors(conn, run_id)
            (
                direct_attempts,
                direct_observations,
                direct_completeness,
                direct_unattributed,
                direct_diagnostics,
            ) = self._collect(conn, (run_id,))
            (
                lineage_attempts,
                lineage_observations,
                lineage_completeness,
                lineage_unattributed,
                lineage_diagnostics,
            ) = self._collect(conn, lineage_ids)
        return RunUsageReport(
            run_id=run_id,
            direct=_aggregate_scope(
                scope="direct",
                run_ids=(run_id,),
                attempts=direct_attempts,
                observations=direct_observations,
                diagnostics=direct_diagnostics,
                attempt_completeness=direct_completeness,
                unattributed_attempts=direct_unattributed,
            ),
            recovery_lineage=_aggregate_scope(
                scope="recovery_lineage",
                run_ids=lineage_ids,
                attempts=lineage_attempts,
                observations=lineage_observations,
                diagnostics=lineage_diagnostics,
                attempt_completeness=lineage_completeness,
                unattributed_attempts=lineage_unattributed,
            ),
        )

    def sequence_report(self, sequence_id: str) -> SequenceUsageReport:
        with self.store.begin_read() as conn:
            run_ids = self._sequence_run_ids(conn, sequence_id)
            attempts, observations, completeness, unattributed, diagnostics = self._collect(
                conn, run_ids
            )
        return SequenceUsageReport(
            sequence_id=sequence_id,
            sequence=_aggregate_scope(
                scope="sequence",
                run_ids=run_ids,
                attempts=attempts,
                observations=observations,
                diagnostics=diagnostics,
                attempt_completeness=completeness,
                unattributed_attempts=unattributed,
            ),
        )


def scheduler_usage(
    run_id: str,
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> RunUsageReport:
    return TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db_path or default_engine_db_path()),
        artifact_root=artifact_root,
    ).run_report(run_id)


def scheduler_sequence_usage(
    sequence_id: str,
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> SequenceUsageReport:
    return TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db_path or default_engine_db_path()),
        artifact_root=artifact_root,
    ).sequence_report(sequence_id)


def render_usage_report(report: RunUsageReport | SequenceUsageReport, *, output: str) -> str:
    if output == "json":
        return report.model_dump_json(indent=2) + "\n"
    scopes = (
        (report.direct, report.recovery_lineage)
        if isinstance(report, RunUsageReport)
        else (report.sequence,)
    )
    lines = [
        f"Usage normalization: {report.normalization_version}",
    ]
    for scope in scopes:
        total = "unknown" if scope.known_total_tokens is None else str(scope.known_total_tokens)
        lines.append(
            f"{scope.scope}: known_total_tokens={total}; coverage={scope.coverage}; "
            f"attempts={scope.inference_attempts}"
        )
        for item in scope.breakdowns:
            item_total = (
                "unknown" if item.known_total_tokens is None else str(item.known_total_tokens)
            )
            lines.append(
                f"  {item.provider}/{item.role}/{item.stage}: "
                f"known_total_tokens={item_total}; coverage={item.coverage}"
            )
    return "\n".join(lines) + "\n"
