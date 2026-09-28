"""Read-only integration process output inspection service."""

# mypy: disable-error-code=call-arg

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.models import IntegrationProcessOutputChunk
from ai_dev_loop.integration_api.process_output_auth import ProcessOutputIntegrityError
from ai_dev_loop.integration_api.process_output_projection import build_process_output_chunk
from ai_dev_loop.integration_api.process_output_reader import (
    CaptureSnapshot,
    read_process_output_chunk,
)
from ai_dev_loop.integration_api.process_output_resolution import (
    ProcessStreamName,
    authenticate_for_output,
    load_stream_truncation,
    resolve_confined_stream_path,
    resolve_process_stream,
    stream_availability_reason,
    timeline_status,
)
from ai_dev_loop.integration_api.review_projection import confined_run_root
from ai_dev_loop.integration_api.run_service import _map_engine_error
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.infrastructure.paths import default_artifact_root, default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


class IntegrationProcessOutputReadService:
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

    def read_process_output(
        self,
        run_id: str,
        *,
        attempt_id: str,
        stream: ProcessStreamName,
        byte_offset: int = 0,
        limit: int = 65536,
    ) -> IntegrationProcessOutputChunk:
        self._require_scheduler_database()
        try:
            with self.store.begin_read() as conn:
                self.store.load_validated_snapshot(conn, run_id)
                attempt = self.store.get_attempt_by_id(conn, attempt_id)
                if attempt is None:
                    raise IntegrationApiError.not_found("Attempt not found.")
                if str(attempt["run_id"]) != run_id:
                    raise IntegrationApiError.not_found("Attempt not found.")
                dispatch = self.store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
                effect_kind = str(dispatch["effect_kind"]) if dispatch is not None else ""
                run_root = confined_run_root(self.artifact_root, run_id)
                auth = authenticate_for_output(run_root, attempt, effect_kind)
                try:
                    resolved = resolve_process_stream(
                        run_root,
                        attempt_row=attempt,
                        effect_kind=effect_kind,
                        stream=stream,
                        auth=auth,
                    )
                except ProcessOutputIntegrityError as exc:
                    raise IntegrationApiError.data_integrity(
                        "Process output stream failed verification."
                    ) from exc
                try:
                    reason = stream_availability_reason(
                        run_root,
                        resolved=resolved,
                        auth=auth,
                        attempt_status=str(attempt["status"]),
                    )
                except ProcessOutputIntegrityError as exc:
                    raise IntegrationApiError.data_integrity(
                        "Process output stream failed verification."
                    ) from exc
                if reason is not None:
                    return build_process_output_chunk(
                        run_id=run_id,
                        attempt_id=attempt_id,
                        component=resolved.component,
                        stream=stream,
                        stream_format=resolved.stream_format,
                        process_state=timeline_status(str(attempt["status"])),
                        complete=False,
                        truncated_at_source=None,
                        unavailable_reason=reason,
                    )
                path = resolve_confined_stream_path(run_root, resolved.relative_path)
                pre_stat = path.stat()
                snapshot = CaptureSnapshot(
                    inode=pre_stat.st_ino,
                    mtime_ns=pre_stat.st_mtime_ns,
                    size=pre_stat.st_size,
                )
                size_before = pre_stat.st_size
                try:
                    truncation = load_stream_truncation(
                        run_root,
                        attempt_row=attempt,
                        effect_kind=effect_kind,
                        stream=stream,
                        resolved=resolved,
                        auth=auth,
                    )
                except ProcessOutputIntegrityError as exc:
                    raise IntegrationApiError.data_integrity(
                        "Process output metadata failed verification."
                    ) from exc
                chunk = read_process_output_chunk(
                    path,
                    byte_offset=byte_offset,
                    limit=limit,
                    writer_stopped=auth.writer_stopped,
                    size_before_read=size_before,
                    snapshot=snapshot,
                )
                return build_process_output_chunk(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    component=resolved.component,
                    stream=stream,
                    stream_format=resolved.stream_format,
                    media_type=resolved.media_type,
                    process_state=timeline_status(str(attempt["status"])),
                    complete=chunk.complete,
                    truncated_at_source=truncation.truncated_at_source,
                    stored_bytes=truncation.stored_bytes,
                    chunk=chunk,
                )
        except IntegrationApiError:
            raise
        except SchedulerEngineError as exc:
            raise _map_engine_error(exc) from exc


def default_process_output_read_service() -> IntegrationProcessOutputReadService:
    return IntegrationProcessOutputReadService(
        SqliteSchedulerStore.open_readonly(default_engine_db_path())
    )
