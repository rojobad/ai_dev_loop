"""Resolve scheduler attempt child stdout/stderr capture paths for integration reads."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.process_output_auth import (
    AuthenticatedProcessAttempt,
    ProcessOutputIntegrityError,
    assert_confined_artifact_path,
    authenticate_process_attempt,
    writer_stopped_for_status,
)
from ai_dev_loop.runners.cursor import CREATE_CHAT_STDERR_REL, CREATE_CHAT_STDOUT_REL
from ai_dev_loop.scheduler.application.attempt_envelope import sha256_file
from ai_dev_loop.scheduler.application.output_observation import (
    OutputObservationError,
    load_cursor_output_observation,
)
from ai_dev_loop.scheduler.domain.codex_contract import (
    CODEX_ATTEMPT_EFFECT_KINDS,
    codex_attempt_events_rel,
    codex_attempt_stderr_rel,
    codex_review_metadata_rel,
)
from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
    cursor_attempt_events_rel,
    cursor_attempt_stderr_rel,
)
from ai_dev_loop.scheduler.infrastructure.paths import resolve_run_relative_path

ProcessStreamName = Literal["stdout", "stderr"]
ProcessOutputFormat = Literal["jsonl", "text"]
AttemptTimelineStatus = Literal[
    "launching",
    "active",
    "completed",
    "failed",
    "cancelled",
    "uncertain",
]


@dataclass(frozen=True)
class ResolvedProcessStream:
    relative_path: str
    media_type: str
    stream_format: ProcessOutputFormat
    component: str
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class ProcessStreamTruncation:
    truncated_at_source: bool | None
    stored_bytes: int | None = None


def timeline_status(raw_status: str) -> AttemptTimelineStatus:
    if raw_status in {"launching", "active"}:
        return raw_status  # type: ignore[return-value]
    if raw_status in {"completed", "failed", "cancelled", "uncertain"}:
        return raw_status  # type: ignore[return-value]
    return "uncertain"


def attempt_is_resolved(raw_status: str) -> bool:
    return writer_stopped_for_status(raw_status)


def _cursor_iteration(auth: AuthenticatedProcessAttempt) -> int | None:
    if auth.outcome is not None:
        iteration = auth.outcome.get("iteration")
        if isinstance(iteration, int) and iteration >= 1:
            return iteration
    iteration = auth.invocation.get("iteration")
    if isinstance(iteration, int) and iteration >= 1:
        return iteration
    return None


def _codex_iteration(auth: AuthenticatedProcessAttempt) -> int | None:
    if auth.outcome is not None:
        iteration = auth.outcome.get("review_iteration")
        if isinstance(iteration, int) and iteration >= 1:
            return iteration
    iteration = auth.invocation.get("review_iteration")
    if isinstance(iteration, int) and iteration >= 1:
        return iteration
    return None


def _create_chat_stream_binding(
    run_root: Path,
    auth: AuthenticatedProcessAttempt,
    stream: ProcessStreamName,
) -> str | None:
    """Return confined child capture rel when outcome binds bytes to this attempt."""
    if auth.outcome is None:
        return None
    if str(auth.outcome.get("effect_kind", "")) != CREATE_CHAT_EFFECT_KIND:
        return None
    if stream == "stdout":
        rel = str(auth.outcome.get("create_chat_stdout_artifact_path", "")).strip()
        expected_sha = str(auth.outcome.get("create_chat_stdout_sha256", "")).strip()
        canonical_rel = CREATE_CHAT_STDOUT_REL
    else:
        rel = str(auth.outcome.get("create_chat_stderr_artifact_path", "")).strip()
        expected_sha = str(auth.outcome.get("create_chat_stderr_sha256", "")).strip()
        canonical_rel = CREATE_CHAT_STDERR_REL
    if rel != canonical_rel or not expected_sha:
        return None
    path = assert_confined_artifact_path(run_root, rel)
    if not path.is_file():
        raise ProcessOutputIntegrityError("create-chat child capture is missing")
    if sha256_file(path) != expected_sha:
        raise ProcessOutputIntegrityError("create-chat child capture hash mismatch")
    return rel


def resolve_process_stream(
    run_root: Path,
    *,
    attempt_row: sqlite3.Row,
    effect_kind: str,
    stream: ProcessStreamName,
    auth: AuthenticatedProcessAttempt,
) -> ResolvedProcessStream:
    component = str(attempt_row["component"])
    attempt_id = str(attempt_row["attempt_id"])

    if effect_kind == RUN_CURSOR_TURN_EFFECT_KIND:
        iteration = _cursor_iteration(auth)
        if iteration is None:
            return ResolvedProcessStream(
                "",
                "",
                "text",
                "cursor",
                unavailable_reason="not_captured",
            )
        if stream == "stdout":
            rel = cursor_attempt_events_rel(iteration, attempt_id)
            return ResolvedProcessStream(rel, "application/x-ndjson", "jsonl", "cursor")
        rel = cursor_attempt_stderr_rel(iteration, attempt_id)
        return ResolvedProcessStream(rel, "text/plain", "text", "cursor")

    if effect_kind == CREATE_CHAT_EFFECT_KIND:
        bound_rel = _create_chat_stream_binding(run_root, auth, stream)
        if bound_rel is None:
            return ResolvedProcessStream(
                "",
                "",
                "text",
                "cursor",
                unavailable_reason="not_captured",
            )
        media = "text/plain"
        return ResolvedProcessStream(bound_rel, media, "text", "cursor")

    if effect_kind in CODEX_ATTEMPT_EFFECT_KINDS:
        iteration = _codex_iteration(auth)
        if iteration is None:
            return ResolvedProcessStream(
                "",
                "",
                "text",
                "codex",
                unavailable_reason="not_captured",
            )
        if stream == "stdout":
            rel = codex_attempt_events_rel(iteration, attempt_id)
            return ResolvedProcessStream(rel, "application/x-ndjson", "jsonl", "codex")
        rel = codex_attempt_stderr_rel(iteration, attempt_id)
        return ResolvedProcessStream(rel, "text/plain", "text", "codex")

    return ResolvedProcessStream(
        "",
        "",
        "text",
        component,
        unavailable_reason="not_captured",
    )


def stream_availability_reason(
    run_root: Path,
    *,
    resolved: ResolvedProcessStream,
    auth: AuthenticatedProcessAttempt,
    attempt_status: str,
) -> str | None:
    if resolved.unavailable_reason:
        return resolved.unavailable_reason
    if not resolved.relative_path:
        return "not_captured"
    try:
        path = resolve_run_relative_path(run_root, resolved.relative_path)
    except ValueError as exc:
        raise ProcessOutputIntegrityError("process output path is not safe to read") from exc
    if path.is_symlink():
        raise ProcessOutputIntegrityError("process output path is not safe to read")
    if path.is_file():
        return None
    if attempt_status in {"launching", "active"}:
        return "not_yet_produced"
    if auth.writer_stopped:
        raise ProcessOutputIntegrityError("finalized child stream is missing")
    if attempt_status == "cancelled":
        return "not_yet_produced"
    if attempt_status == "uncertain":
        return "not_yet_produced"
    return "not_captured"


def _strict_bool_field(metadata: dict[str, object], key: str) -> bool | None:
    if key not in metadata:
        return None
    value = metadata[key]
    if isinstance(value, bool):
        return value
    raise ProcessOutputIntegrityError(f"codex capture metadata field {key} is invalid")


def _strict_int_field(metadata: dict[str, object], key: str) -> int | None:
    if key not in metadata:
        return None
    value = metadata[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProcessOutputIntegrityError(f"codex capture metadata field {key} is invalid")
    return value


def load_stream_truncation(
    run_root: Path,
    *,
    attempt_row: sqlite3.Row,
    effect_kind: str,
    stream: ProcessStreamName,
    resolved: ResolvedProcessStream,
    auth: AuthenticatedProcessAttempt,
) -> ProcessStreamTruncation:
    attempt_id = str(attempt_row["attempt_id"])
    run_id = str(attempt_row["run_id"])
    if not auth.writer_stopped:
        return ProcessStreamTruncation(truncated_at_source=None)

    path: Path | None = None
    if resolved.relative_path:
        try:
            path = resolve_run_relative_path(run_root, resolved.relative_path)
        except ValueError as exc:
            raise ProcessOutputIntegrityError("process output path is not safe to read") from exc

    if effect_kind == RUN_CURSOR_TURN_EFFECT_KIND:
        iteration = _cursor_iteration(auth)
        if iteration is None:
            return ProcessStreamTruncation(truncated_at_source=None)
        try:
            observation = load_cursor_output_observation(
                run_root,
                run_id=run_id,
                attempt_id=attempt_id,
                iteration=iteration,
            )
        except OutputObservationError as exc:
            raise ProcessOutputIntegrityError(str(exc)) from exc
        if observation is None:
            return ProcessStreamTruncation(truncated_at_source=None)
        side = observation.stdout if stream == "stdout" else observation.stderr
        if path is not None and path.is_file():
            on_disk = path.stat().st_size
            if side.stored_bytes != on_disk:
                raise ProcessOutputIntegrityError("cursor output observation size mismatch")
        return ProcessStreamTruncation(
            truncated_at_source=side.truncated,
            stored_bytes=side.stored_bytes,
        )

    if effect_kind in CODEX_ATTEMPT_EFFECT_KINDS:
        iteration = _codex_iteration(auth)
        if iteration is None:
            return ProcessStreamTruncation(truncated_at_source=None)
        truncated: bool | None = None
        stored: int | None = None
        metadata_rel = codex_review_metadata_rel(iteration, attempt_id)
        try:
            meta_path = resolve_run_relative_path(run_root, metadata_rel)
        except ValueError as exc:
            raise ProcessOutputIntegrityError("codex capture metadata path invalid") from exc
        if meta_path.is_file() and not meta_path.is_symlink():
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProcessOutputIntegrityError("codex capture metadata invalid") from exc
            if not isinstance(metadata, dict):
                raise ProcessOutputIntegrityError("codex capture metadata invalid")
            if stream == "stdout":
                truncated = _strict_bool_field(metadata, "stdout_truncated")
                stored = _strict_int_field(metadata, "stdout_captured_bytes")
            else:
                truncated = _strict_bool_field(metadata, "stderr_truncated")
                stored = _strict_int_field(metadata, "stderr_captured_bytes")
        if auth.outcome is not None:
            if stream == "stdout":
                outcome_trunc_key = "stdout_truncated"
                outcome_bytes_key = "stdout_captured_bytes"
            else:
                outcome_trunc_key = "stderr_truncated"
                outcome_bytes_key = "stderr_captured_bytes"
            if outcome_trunc_key in auth.outcome:
                outcome_flag = auth.outcome.get(outcome_trunc_key)
                if not isinstance(outcome_flag, bool):
                    raise ProcessOutputIntegrityError("codex outcome truncation field is invalid")
                if truncated is None:
                    truncated = outcome_flag
                elif truncated != outcome_flag:
                    raise ProcessOutputIntegrityError(
                        "codex truncation metadata disagrees with outcome"
                    )
            if outcome_bytes_key in auth.outcome:
                outcome_bytes = auth.outcome.get(outcome_bytes_key)
                if isinstance(outcome_bytes, bool) or not isinstance(outcome_bytes, int):
                    raise ProcessOutputIntegrityError(
                        "codex outcome captured bytes field is invalid"
                    )
                if stored is None:
                    stored = outcome_bytes
                elif stored != outcome_bytes:
                    raise ProcessOutputIntegrityError(
                        "codex captured bytes metadata disagrees with outcome"
                    )
        return ProcessStreamTruncation(truncated_at_source=truncated, stored_bytes=stored)

    return ProcessStreamTruncation(truncated_at_source=None)


def resolve_confined_stream_path(run_root: Path, relative_path: str) -> Path:
    try:
        path = resolve_run_relative_path(run_root, relative_path)
    except ValueError as exc:
        raise IntegrationApiError.data_integrity(
            "Process output path is not safe to read."
        ) from exc
    if path.is_symlink():
        raise IntegrationApiError.data_integrity("Process output path is not safe to read.")
    try:
        path.resolve(strict=False).relative_to(run_root.resolve(strict=False))
    except ValueError as exc:
        raise IntegrationApiError.data_integrity("Process output path escapes run root.") from exc
    return path


def authenticate_for_output(
    run_root: Path,
    attempt_row: sqlite3.Row,
    effect_kind: str,
) -> AuthenticatedProcessAttempt:
    try:
        return authenticate_process_attempt(
            run_root,
            attempt_row,
            effect_kind=effect_kind,
        )
    except ProcessOutputIntegrityError as exc:
        raise IntegrationApiError.data_integrity(
            "Process output attempt evidence failed verification."
        ) from exc
