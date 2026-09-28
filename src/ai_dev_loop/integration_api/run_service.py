"""Read-only integration run inspection service."""

# mypy: disable-error-code=call-arg

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.integration_api.artifact_reader import (
    FrozenArtifactReadError,
    map_frozen_artifact_error,
    read_frozen_run_artifact_chunk,
)
from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.models import (
    IntegrationAttemptListData,
    IntegrationFrozenArtifactChunk,
    IntegrationHistoryListData,
    IntegrationReviewBudget,
    IntegrationRunInspectData,
    IntegrationRunListData,
    IntegrationRunListItem,
)
from ai_dev_loop.integration_api.run_projection import (
    RunKindFilter,
    _sequence_binding,
    build_attempt_item,
    build_history_item,
    build_run_inspect_data,
    build_run_list_item,
    collection_page,
    frozen_chunk_to_wire,
)
from ai_dev_loop.integration_api.validation import (
    COLLECTION_DEFAULT_LIMIT,
    validate_collection_bounds,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.review_budget import (
    load_review_budget_extensions,
    review_budget_projection,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_artifact_root, default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _map_engine_error(exc: SchedulerEngineError) -> IntegrationApiError:
    if exc.kind is SchedulerEngineErrorKind.NOT_FOUND:
        return IntegrationApiError.not_found("Run not found.")
    if exc.kind in {SchedulerEngineErrorKind.VALIDATION, SchedulerEngineErrorKind.SCHEMA}:
        return IntegrationApiError.invalid_argument(str(exc))
    if exc.kind is SchedulerEngineErrorKind.CORRUPTION:
        return IntegrationApiError.data_integrity("Scheduler run state is inconsistent.")
    return IntegrationApiError.internal()


class IntegrationRunReadService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        *,
        artifact_root: Path | None = None,
    ) -> None:
        self.store = store
        self.artifact_root = artifact_root or default_artifact_root()

    def _require_scheduler_database(self) -> None:
        if not self.store.db_path.exists():
            raise IntegrationApiError.not_found("Scheduler database not found.")

    def list_runs(
        self,
        *,
        kind: RunKindFilter = "all",
        offset: int = 0,
        limit: int = COLLECTION_DEFAULT_LIMIT,
    ) -> IntegrationRunListData:
        validate_collection_bounds(offset, limit)
        if not self.store.db_path.exists():
            return IntegrationRunListData(
                items=(),
                page=collection_page(offset, limit, False),
            )
        items: list[IntegrationRunListItem] = []
        try:
            with self.store.begin_read() as conn:
                run_ids, has_more = self.store.list_run_ids_for_integration(
                    conn,
                    kind=kind,
                    offset=offset,
                    limit=limit,
                )
                for run_id in run_ids:
                    state, _, _ = self.store.load_validated_snapshot(conn, run_id)
                    row = self.store.get_run_row(conn, run_id)
                    ledger_reviews_completed = self.store.count_review_completion_events(
                        conn,
                        run_id,
                    )
                    extension_events = load_review_budget_extensions(self.store, conn, run_id)
                    reviews_completed, max_reviews, submitted_max = review_budget_projection(
                        state,
                        extension_events,
                        ledger_reviews_completed=ledger_reviews_completed,
                    )
                    review_budget = IntegrationReviewBudget(
                        completed=reviews_completed,
                        max_reviews=max_reviews,
                        submitted_max=submitted_max,
                    )
                    items.append(
                        build_run_list_item(
                            state=state,
                            submitted_at=str(state.submitted_at),
                            updated_at=str(row["updated_at"]),
                            review_budget=review_budget,
                            sequence_binding=_sequence_binding(state.context.sequence),
                        )
                    )
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc
        return IntegrationRunListData(
            items=tuple(items),
            page=collection_page(offset, limit, has_more),
        )

    def inspect_run(self, run_id: str) -> IntegrationRunInspectData:
        self._require_scheduler_database()
        try:
            with self.store.begin_read() as conn:
                state, _, _ = self.store.load_validated_snapshot(conn, run_id)
                row = self.store.get_run_row(conn, run_id)
                return build_run_inspect_data(
                    self.store,
                    conn,
                    state,
                    submitted_at=str(state.submitted_at),
                    updated_at=str(row["updated_at"]),
                )
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc

    def list_attempts(
        self,
        run_id: str,
        *,
        offset: int = 0,
        limit: int = COLLECTION_DEFAULT_LIMIT,
    ) -> IntegrationAttemptListData:
        validate_collection_bounds(offset, limit)
        self._require_scheduler_database()
        try:
            with self.store.begin_read() as conn:
                self.store.load_validated_snapshot(conn, run_id)
                if not self.store.schema_supports_integration_attempt_queries(conn):
                    raise IntegrationApiError.unsupported(
                        "Attempt history is unavailable for this scheduler database version.",
                    )
                rows, has_more = self.store.list_integration_attempt_rows(
                    conn,
                    run_id,
                    offset=offset,
                    limit=limit,
                )
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc
        return IntegrationAttemptListData(
            run_id=run_id,
            items=tuple(build_attempt_item(row) for row in rows),
            page=collection_page(offset, limit, has_more),
        )

    def list_timeline(
        self,
        run_id: str,
        *,
        offset: int = 0,
        limit: int = COLLECTION_DEFAULT_LIMIT,
    ) -> IntegrationAttemptListData:
        return self.list_attempts(run_id, offset=offset, limit=limit)

    def list_history(
        self,
        run_id: str,
        *,
        offset: int = 0,
        limit: int = COLLECTION_DEFAULT_LIMIT,
    ) -> IntegrationHistoryListData:
        validate_collection_bounds(offset, limit)
        self._require_scheduler_database()
        try:
            with self.store.begin_read() as conn:
                self.store.load_validated_snapshot(conn, run_id)
                if not self.store.schema_supports_integration_history_queries(conn):
                    raise IntegrationApiError.unsupported(
                        "Event history is unavailable for this scheduler database version.",
                    )
                rows, has_more = self.store.list_events_for_run_offset(
                    conn,
                    run_id,
                    offset=offset,
                    limit=limit,
                )
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc
        return IntegrationHistoryListData(
            run_id=run_id,
            items=tuple(build_history_item(row) for row in rows),
            page=collection_page(offset, limit, has_more),
        )

    def read_plan_chunk(
        self,
        run_id: str,
        *,
        byte_offset: int = 0,
        limit: int = 65536,
    ) -> IntegrationFrozenArtifactChunk:
        return self._read_frozen_chunk(run_id, "plan", byte_offset=byte_offset, limit=limit)

    def read_initial_prompt_chunk(
        self,
        run_id: str,
        *,
        byte_offset: int = 0,
        limit: int = 65536,
    ) -> IntegrationFrozenArtifactChunk:
        return self._read_frozen_chunk(
            run_id,
            "initial_prompt",
            byte_offset=byte_offset,
            limit=limit,
        )

    def _read_frozen_chunk(
        self,
        run_id: str,
        artifact_kind: str,
        *,
        byte_offset: int,
        limit: int,
    ) -> IntegrationFrozenArtifactChunk:
        self._require_scheduler_database()
        try:
            with self.store.begin_read() as conn:
                state, _, _ = self.store.load_validated_snapshot(conn, run_id)
                binding = state.context.plan_prompt
                if artifact_kind == "plan":
                    relative_path = binding.plan_artifact_path
                    expected_sha256 = binding.plan_sha256
                    source_repository_path = binding.plan_repository_path
                elif artifact_kind == "initial_prompt":
                    relative_path = binding.prompt_artifact_path
                    expected_sha256 = binding.prompt_sha256
                    source_repository_path = binding.prompt_source_repository_path
                else:
                    raise IntegrationApiError.invalid_argument("Unsupported artifact kind.")
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc
        try:
            chunk = read_frozen_run_artifact_chunk(
                artifact_root=self.artifact_root,
                run_id=run_id,
                artifact_kind=artifact_kind,
                relative_path=relative_path,
                source_repository_path=source_repository_path,
                expected_sha256=expected_sha256,
                byte_offset=byte_offset,
                limit=limit,
            )
        except (FrozenArtifactReadError, OSError) as exc:
            raise map_frozen_artifact_error(exc) from exc
        return frozen_chunk_to_wire(chunk)


def default_run_read_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> IntegrationRunReadService:
    path = db_path or default_engine_db_path()
    if not path.exists():
        store = SqliteSchedulerStore.__new__(SqliteSchedulerStore)
        store.db_path = path
        store.busy_timeout_ms = 5000
        store._migration_fault_hook = None
        store._read_only = True
        from ai_dev_loop.scheduler.infrastructure.paths import apply_database_permissions

        store._apply_database_permissions = apply_database_permissions
        return IntegrationRunReadService(store, artifact_root=artifact_root)
    store = SqliteSchedulerStore.open_readonly(path)
    return IntegrationRunReadService(store, artifact_root=artifact_root)
