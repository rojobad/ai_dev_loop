"""Wire projections for process output inspection."""

# mypy: disable-error-code=call-arg

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.integration_api.models import (
    IntegrationProcessOutputChunk,
    IntegrationProcessStreamAvailability,
)
from ai_dev_loop.integration_api.process_output_reader import ProcessOutputChunk
from ai_dev_loop.integration_api.process_output_resolution import (
    AttemptTimelineStatus,
    ProcessOutputFormat,
    ProcessStreamName,
    ResolvedProcessStream,
    stream_availability_reason,
)


def build_process_output_chunk(
    *,
    run_id: str,
    attempt_id: str,
    component: str,
    stream: ProcessStreamName,
    stream_format: ProcessOutputFormat,
    process_state: AttemptTimelineStatus,
    complete: bool,
    truncated_at_source: bool | None,
    unavailable_reason: str | None = None,
    media_type: str | None = None,
    stored_bytes: int | None = None,
    chunk: ProcessOutputChunk | None = None,
) -> IntegrationProcessOutputChunk:
    if unavailable_reason is not None:
        return IntegrationProcessOutputChunk(
            available=False,
            reason=unavailable_reason,
            media_type=None,
            byte_offset=0,
            returned_bytes=0,
            next_offset=None,
            available_bytes=None,
            has_more=False,
            content_base64="",
            sha256=None,
            run_id=run_id,
            attempt_id=attempt_id,
            component=component,
            stream=stream,
            stream_format=stream_format,
            process_state=process_state,
            complete=complete,
            truncated_at_source=truncated_at_source,
            stored_bytes=stored_bytes,
        )
    assert chunk is not None
    return IntegrationProcessOutputChunk(
        available=True,
        reason=None,
        media_type=media_type,
        byte_offset=chunk.byte_offset,
        returned_bytes=chunk.returned_bytes,
        next_offset=chunk.next_offset,
        available_bytes=chunk.available_bytes,
        has_more=chunk.has_more,
        content_base64=chunk.content_base64,
        sha256=None,
        run_id=run_id,
        attempt_id=attempt_id,
        component=component,
        stream=stream,
        stream_format=stream_format,
        process_state=process_state,
        complete=complete,
        truncated_at_source=truncated_at_source,
        stored_bytes=stored_bytes,
    )


def build_stream_availability(
    run_root: Path,
    *,
    resolved: ResolvedProcessStream,
    attempt_status: str,
    stream: ProcessStreamName,
) -> IntegrationProcessStreamAvailability:
    reason = stream_availability_reason(
        run_root,
        resolved=resolved,
        attempt_status=attempt_status,
    )
    if reason is None:
        return IntegrationProcessStreamAvailability(available=True, reason=None, stream=stream)
    return IntegrationProcessStreamAvailability(available=False, reason=reason, stream=stream)


def build_process_output_availability(
    run_root: Path,
    *,
    stdout_resolved: ResolvedProcessStream,
    stderr_resolved: ResolvedProcessStream,
    attempt_status: str,
) -> tuple[IntegrationProcessStreamAvailability, IntegrationProcessStreamAvailability]:
    return (
        build_stream_availability(
            run_root,
            resolved=stdout_resolved,
            attempt_status=attempt_status,
            stream="stdout",
        ),
        build_stream_availability(
            run_root,
            resolved=stderr_resolved,
            attempt_status=attempt_status,
            stream="stderr",
        ),
    )
