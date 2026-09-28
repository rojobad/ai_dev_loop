"""Read-only integration sequence inspection service."""

# mypy: disable-error-code=call-arg

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.integration_api.artifact_reader import (
    FrozenArtifactReadError,
    SequenceCompletionReportMissingError,
    chunk_validated_sequence_report,
    load_integration_sequence_completion_report,
    map_frozen_artifact_error,
    read_frozen_sequence_phase_artifact_chunk,
)
from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.models import (
    IntegrationFrozenSequenceArtifactChunk,
    IntegrationSequenceInspectData,
    IntegrationSequenceListData,
    IntegrationSequenceListItem,
    IntegrationSequencePhaseRunListData,
    IntegrationSequenceReportChunk,
)
from ai_dev_loop.integration_api.run_projection import collection_page, frozen_chunk_to_wire
from ai_dev_loop.integration_api.sequence_projection import (
    build_sequence_inspect_data,
    build_sequence_list_item,
    build_sequence_phase_runs_data,
)
from ai_dev_loop.integration_api.validation import (
    COLLECTION_DEFAULT_LIMIT,
    validate_artifact_read_bounds,
    validate_collection_bounds,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.domain.sequence import (
    AwaitingFinalizationSequenceState,
    FrozenSequenceEntry,
)
from ai_dev_loop.scheduler.domain.sequence_run_lineage import SequenceRunLineageValidationError
from ai_dev_loop.scheduler.infrastructure.paths import (
    default_artifact_root,
    default_engine_db_path,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _map_engine_error(exc: SchedulerEngineError) -> IntegrationApiError:
    if exc.kind is SchedulerEngineErrorKind.NOT_FOUND:
        return IntegrationApiError.not_found("Sequence not found.")
    if exc.kind in {SchedulerEngineErrorKind.VALIDATION, SchedulerEngineErrorKind.SCHEMA}:
        if "sequence" in str(exc).lower() and "schema" in str(exc).lower():
            return IntegrationApiError.unsupported(
                "Sequence inspection is unavailable for this scheduler database version.",
            )
        return IntegrationApiError.invalid_argument(str(exc))
    if exc.kind is SchedulerEngineErrorKind.CORRUPTION:
        return IntegrationApiError.data_integrity("Scheduler sequence state is inconsistent.")
    return IntegrationApiError.internal()


class IntegrationSequenceReadService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        *,
        artifact_root: Path | None = None,
    ) -> None:
        self.store = store
        self.artifact_root = artifact_root or default_artifact_root()
        self._status = SequenceStatusService(self.store, artifacts=None)

    def _require_scheduler_database(self) -> None:
        if not self.store.db_path.exists():
            raise IntegrationApiError.not_found("Scheduler database not found.")

    def _require_sequence_schema(self, conn: object) -> None:
        import sqlite3

        if not isinstance(conn, sqlite3.Connection):
            raise TypeError("expected sqlite connection")
        if not self.store.schema_supports_integration_sequence_queries(conn):
            raise IntegrationApiError.unsupported(
                "Sequence inspection is unavailable for this scheduler database version.",
            )

    def list_sequences(
        self,
        *,
        offset: int = 0,
        limit: int = COLLECTION_DEFAULT_LIMIT,
    ) -> IntegrationSequenceListData:
        validate_collection_bounds(offset, limit)
        if not self.store.db_path.exists():
            return IntegrationSequenceListData(
                items=(),
                page=collection_page(offset, limit, False),
            )
        items: list[IntegrationSequenceListItem] = []
        try:
            with self.store.begin_read() as conn:
                self._require_sequence_schema(conn)
                sequence_ids, has_more = self.store.list_sequence_ids_for_integration(
                    conn,
                    offset=offset,
                    limit=limit,
                )
                for sequence_id in sequence_ids:
                    status = self._status.get_status(sequence_id)
                    items.append(build_sequence_list_item(status))
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc
        except IntegrationApiError:
            raise
        return IntegrationSequenceListData(
            items=tuple(items),
            page=collection_page(offset, limit, has_more),
        )

    def inspect_sequence(self, sequence_id: str) -> IntegrationSequenceInspectData:
        self._require_scheduler_database()
        try:
            with self.store.begin_read() as conn:
                self._require_sequence_schema(conn)
                status = self._status.get_status(sequence_id)
                return build_sequence_inspect_data(
                    self.store,
                    conn,
                    status,
                    artifact_root=self.artifact_root,
                )
        except SequenceRunLineageValidationError as exc:
            raise IntegrationApiError.data_integrity(str(exc)) from exc
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc

    def list_phase_runs(
        self,
        sequence_id: str,
        *,
        ordinal: int,
        offset: int = 0,
        limit: int = COLLECTION_DEFAULT_LIMIT,
    ) -> IntegrationSequencePhaseRunListData:
        validate_collection_bounds(offset, limit)
        if ordinal < 1:
            raise IntegrationApiError.invalid_argument("ordinal must be >= 1.")
        self._require_scheduler_database()
        try:
            with self.store.begin_read() as conn:
                self._require_sequence_schema(conn)
                status = self._status.get_status(sequence_id)
                return build_sequence_phase_runs_data(
                    self.store,
                    conn,
                    status,
                    ordinal=ordinal,
                    offset=offset,
                    limit=limit,
                )
        except ValueError as exc:
            raise IntegrationApiError.invalid_argument(str(exc)) from exc
        except SequenceRunLineageValidationError as exc:
            raise IntegrationApiError.data_integrity(str(exc)) from exc
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc

    def _definition_entry(
        self,
        conn: object,
        sequence_id: str,
        ordinal: int,
    ) -> FrozenSequenceEntry:
        import sqlite3

        if not isinstance(conn, sqlite3.Connection):
            raise TypeError("expected sqlite connection")
        if ordinal < 1:
            raise IntegrationApiError.invalid_argument("ordinal must be >= 1.")
        definition = self.store.load_validated_sequence_state(conn, sequence_id).definition
        if ordinal > len(definition.entries):
            raise IntegrationApiError.invalid_argument("ordinal out of range.")
        return definition.entries[ordinal - 1]

    def read_phase_plan_chunk(
        self,
        sequence_id: str,
        *,
        ordinal: int,
        byte_offset: int = 0,
        limit: int = 65536,
    ) -> IntegrationFrozenSequenceArtifactChunk:
        self._require_scheduler_database()
        try:
            with self.store.begin_read() as conn:
                self._require_sequence_schema(conn)
                entry = self._definition_entry(conn, sequence_id, ordinal)
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc
        binding = entry.plan_prompt
        return self._read_phase_chunk(
            sequence_id,
            ordinal=ordinal,
            artifact_kind="plan",
            relative_path=binding.plan_artifact_path,
            expected_sha256=binding.plan_sha256,
            source_repository_path=binding.plan_repository_path,
            byte_offset=byte_offset,
            limit=limit,
        )

    def read_phase_prompt_chunk(
        self,
        sequence_id: str,
        *,
        ordinal: int,
        byte_offset: int = 0,
        limit: int = 65536,
    ) -> IntegrationFrozenSequenceArtifactChunk:
        self._require_scheduler_database()
        try:
            with self.store.begin_read() as conn:
                self._require_sequence_schema(conn)
                entry = self._definition_entry(conn, sequence_id, ordinal)
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc
        binding = entry.plan_prompt
        return self._read_phase_chunk(
            sequence_id,
            ordinal=ordinal,
            artifact_kind="initial_prompt",
            relative_path=binding.prompt_artifact_path,
            expected_sha256=binding.prompt_sha256,
            source_repository_path=binding.prompt_source_repository_path,
            byte_offset=byte_offset,
            limit=limit,
        )

    def _read_phase_chunk(
        self,
        sequence_id: str,
        *,
        ordinal: int,
        artifact_kind: str,
        relative_path: str,
        expected_sha256: str,
        source_repository_path: str,
        byte_offset: int,
        limit: int,
    ) -> IntegrationFrozenSequenceArtifactChunk:
        self._require_scheduler_database()
        validate_artifact_read_bounds(byte_offset, limit)
        try:
            with self.store.begin_read() as conn:
                self._require_sequence_schema(conn)
                self.store.load_validated_sequence_state(conn, sequence_id)
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc
        try:
            chunk = read_frozen_sequence_phase_artifact_chunk(
                artifact_root=self.artifact_root,
                sequence_id=sequence_id,
                ordinal=ordinal,
                artifact_kind=artifact_kind,
                relative_path=relative_path,
                source_repository_path=source_repository_path,
                expected_sha256=expected_sha256,
                byte_offset=byte_offset,
                limit=limit,
            )
        except (FrozenArtifactReadError, OSError) as exc:
            raise map_frozen_artifact_error(exc) from exc
        wire = frozen_chunk_to_wire(chunk)
        return IntegrationFrozenSequenceArtifactChunk(
            available=wire.available,
            reason=wire.reason,
            encoding=wire.encoding,
            media_type=wire.media_type,
            byte_offset=wire.byte_offset,
            returned_bytes=wire.returned_bytes,
            next_offset=wire.next_offset,
            available_bytes=wire.available_bytes,
            has_more=wire.has_more,
            content_base64=wire.content_base64,
            sha256=wire.sha256,
            sequence_id=sequence_id,
            ordinal=ordinal,
            artifact_kind=artifact_kind,
            source_repository_path=source_repository_path,
        )

    def read_report_chunk(
        self,
        sequence_id: str,
        *,
        byte_offset: int = 0,
        limit: int = 65536,
    ) -> IntegrationSequenceReportChunk:
        self._require_scheduler_database()
        validate_artifact_read_bounds(byte_offset, limit)
        try:
            with self.store.begin_read() as conn:
                self._require_sequence_schema(conn)
                state = self.store.load_validated_sequence_state(conn, sequence_id)
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc
        if not isinstance(state, AwaitingFinalizationSequenceState):
            return IntegrationSequenceReportChunk(
                available=False,
                reason="not_yet_produced",
                encoding="base64",
                byte_offset=byte_offset,
                returned_bytes=0,
                has_more=False,
                content_base64="",
                sequence_id=sequence_id,
                integrity=None,
            )
        try:
            validated = load_integration_sequence_completion_report(
                artifact_root=self.artifact_root,
                sequence_id=sequence_id,
                expected_sha256=state.completion_report_sha256,
            )
            chunk = chunk_validated_sequence_report(
                validated,
                sequence_id=sequence_id,
                byte_offset=byte_offset,
                limit=limit,
            )
        except SequenceCompletionReportMissingError:
            return IntegrationSequenceReportChunk(
                available=False,
                reason="publication_pending",
                encoding="base64",
                byte_offset=byte_offset,
                returned_bytes=0,
                has_more=False,
                content_base64="",
                sequence_id=sequence_id,
                integrity=None,
            )
        except FrozenArtifactReadError as exc:
            raise map_frozen_artifact_error(exc) from exc
        except OSError:
            raise IntegrationApiError.io_error("Unable to read the sequence report.") from None
        return IntegrationSequenceReportChunk(
            available=True,
            reason=None,
            encoding="base64",
            media_type="application/json",
            byte_offset=chunk.byte_offset,
            returned_bytes=chunk.returned_bytes,
            next_offset=chunk.next_offset,
            available_bytes=chunk.total_bytes,
            has_more=chunk.has_more,
            content_base64=chunk.content_base64,
            sha256=chunk.sha256,
            sequence_id=sequence_id,
            integrity=chunk.integrity,
        )


def default_sequence_read_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> IntegrationSequenceReadService:
    path = db_path or default_engine_db_path()
    if not path.exists():
        store = SqliteSchedulerStore.__new__(SqliteSchedulerStore)
        store.db_path = path
        store.busy_timeout_ms = 5000
        store._migration_fault_hook = None
        store._read_only = True
        from ai_dev_loop.scheduler.infrastructure.paths import apply_database_permissions

        store._apply_database_permissions = apply_database_permissions
        return IntegrationSequenceReadService(store, artifact_root=artifact_root)
    store = SqliteSchedulerStore.open_readonly(path)
    return IntegrationSequenceReadService(store, artifact_root=artifact_root)
