"""Read-only frozen run artifact byte access for the Integration API."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.validation import (
    ARTIFACT_HARD_MAX_LIMIT,
    validate_artifact_read_bounds,
)
from ai_dev_loop.scheduler.domain.sequence import SEQUENCE_COMPLETION_REPORT_ADAPTER
from ai_dev_loop.scheduler.infrastructure.paths import (
    readonly_confined_run_artifact_root,
    readonly_confined_sequence_artifact_root,
    resolve_run_relative_path,
    resolve_sequence_relative_path,
    validate_sha256_hex,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    MAX_PLAN_BYTES,
    MAX_PROMPT_BYTES,
)

_READ_BUFFER_SIZE = 64 * 1024


class FrozenArtifactReadError(Exception):
    """Internal artifact read failure before mapping to IntegrationApiError."""


class SequenceCompletionReportMissingError(Exception):
    """No confined completion report artifact is stored for the sequence."""


@dataclass(frozen=True)
class VerifiedArtifactChunk:
    run_id: str
    artifact_kind: str
    source_repository_path: str
    byte_offset: int
    returned_bytes: int
    total_bytes: int
    next_offset: int | None
    has_more: bool
    content_base64: str
    sha256: str
    media_type: str


def _artifact_max_bytes(artifact_kind: str) -> int:
    if artifact_kind == "plan":
        return MAX_PLAN_BYTES
    if artifact_kind == "initial_prompt":
        return MAX_PROMPT_BYTES
    raise FrozenArtifactReadError("unsupported artifact kind")


def _media_type_for_kind(artifact_kind: str) -> str:
    if artifact_kind == "plan":
        return "text/markdown"
    if artifact_kind == "initial_prompt":
        return "text/plain"
    raise FrozenArtifactReadError("unsupported artifact kind")


def _resolve_existing_artifact_path(
    artifact_root: Path,
    run_id: str,
    relative_path: str,
) -> Path:
    try:
        root = readonly_confined_run_artifact_root(artifact_root, run_id)
        path = resolve_run_relative_path(root, relative_path)
    except ValueError as exc:
        message = str(exc)
        if "symlink" in message or "escapes" in message:
            raise FrozenArtifactReadError("artifact path escapes confined root") from exc
        raise FrozenArtifactReadError("artifact path invalid") from exc
    if not path.is_file():
        raise FrozenArtifactReadError("artifact missing")
    if path.is_symlink():
        raise FrozenArtifactReadError("artifact must not be a symlink")
    if os.name != "nt":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise FrozenArtifactReadError("artifact has unsafe permissions")
    return path


def _verify_size_and_digest(path: Path, expected_sha256: str, max_bytes: int) -> int:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(_READ_BUFFER_SIZE)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise FrozenArtifactReadError("artifact exceeds size limit")
            digest.update(block)
    if digest.hexdigest() != expected_sha256:
        raise FrozenArtifactReadError("artifact hash mismatch")
    return total


def read_frozen_run_artifact_chunk(
    *,
    artifact_root: Path,
    run_id: str,
    artifact_kind: str,
    relative_path: str,
    source_repository_path: str,
    expected_sha256: str,
    byte_offset: int,
    limit: int = 65536,
) -> VerifiedArtifactChunk:
    validate_artifact_read_bounds(byte_offset, limit)
    try:
        validate_sha256_hex(expected_sha256)
    except ValueError as exc:
        raise FrozenArtifactReadError("expected hash invalid") from exc
    max_bytes = _artifact_max_bytes(artifact_kind)
    try:
        path = _resolve_existing_artifact_path(artifact_root, run_id, relative_path)
        total_bytes = _verify_size_and_digest(path, expected_sha256, max_bytes)
    except OSError:
        raise
    except FrozenArtifactReadError:
        raise
    if byte_offset > total_bytes:
        raise IntegrationApiError.invalid_argument("byteOffset is beyond the artifact size.")
    if byte_offset == total_bytes:
        return VerifiedArtifactChunk(
            run_id=run_id,
            artifact_kind=artifact_kind,
            source_repository_path=source_repository_path,
            byte_offset=byte_offset,
            returned_bytes=0,
            total_bytes=total_bytes,
            next_offset=None,
            has_more=False,
            content_base64="",
            sha256=expected_sha256,
            media_type=_media_type_for_kind(artifact_kind),
        )
    read_limit = min(limit, ARTIFACT_HARD_MAX_LIMIT, total_bytes - byte_offset)
    with path.open("rb") as handle:
        handle.seek(byte_offset)
        chunk = handle.read(read_limit)
    next_offset = byte_offset + len(chunk)
    has_more = next_offset < total_bytes
    return VerifiedArtifactChunk(
        run_id=run_id,
        artifact_kind=artifact_kind,
        source_repository_path=source_repository_path,
        byte_offset=byte_offset,
        returned_bytes=len(chunk),
        total_bytes=total_bytes,
        next_offset=next_offset if has_more else None,
        has_more=has_more,
        content_base64=base64.b64encode(chunk).decode("ascii"),
        sha256=expected_sha256,
        media_type=_media_type_for_kind(artifact_kind),
    )


def _resolve_existing_sequence_artifact_path(
    artifact_root: Path,
    sequence_id: str,
    relative_path: str,
) -> Path:
    try:
        root = readonly_confined_sequence_artifact_root(artifact_root, sequence_id)
        path = resolve_sequence_relative_path(root, relative_path)
    except ValueError as exc:
        message = str(exc)
        if "symlink" in message or "escapes" in message:
            raise FrozenArtifactReadError("artifact path escapes confined root") from exc
        raise FrozenArtifactReadError("artifact path invalid") from exc
    if not path.is_file():
        raise FrozenArtifactReadError("artifact missing")
    if path.is_symlink():
        raise FrozenArtifactReadError("artifact must not be a symlink")
    if os.name != "nt":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise FrozenArtifactReadError("artifact has unsafe permissions")
    return path


def read_frozen_sequence_phase_artifact_chunk(
    *,
    artifact_root: Path,
    sequence_id: str,
    ordinal: int,
    artifact_kind: str,
    relative_path: str,
    source_repository_path: str,
    expected_sha256: str,
    byte_offset: int,
    limit: int = 65536,
) -> VerifiedArtifactChunk:
    validate_artifact_read_bounds(byte_offset, limit)
    if ordinal < 1:
        raise FrozenArtifactReadError("ordinal invalid")
    try:
        validate_sha256_hex(expected_sha256)
    except ValueError as exc:
        raise FrozenArtifactReadError("expected hash invalid") from exc
    max_bytes = _artifact_max_bytes(artifact_kind)
    try:
        path = _resolve_existing_sequence_artifact_path(artifact_root, sequence_id, relative_path)
        total_bytes = _verify_size_and_digest(path, expected_sha256, max_bytes)
    except OSError:
        raise
    except FrozenArtifactReadError:
        raise
    if byte_offset > total_bytes:
        raise IntegrationApiError.invalid_argument("byteOffset is beyond the artifact size.")
    if byte_offset == total_bytes:
        return VerifiedArtifactChunk(
            run_id=f"{sequence_id}:{ordinal}",
            artifact_kind=artifact_kind,
            source_repository_path=source_repository_path,
            byte_offset=byte_offset,
            returned_bytes=0,
            total_bytes=total_bytes,
            next_offset=None,
            has_more=False,
            content_base64="",
            sha256=expected_sha256,
            media_type=_media_type_for_kind(artifact_kind),
        )
    read_limit = min(limit, ARTIFACT_HARD_MAX_LIMIT, total_bytes - byte_offset)
    with path.open("rb") as handle:
        handle.seek(byte_offset)
        chunk = handle.read(read_limit)
    next_offset = byte_offset + len(chunk)
    has_more = next_offset < total_bytes
    return VerifiedArtifactChunk(
        run_id=f"{sequence_id}:{ordinal}",
        artifact_kind=artifact_kind,
        source_repository_path=source_repository_path,
        byte_offset=byte_offset,
        returned_bytes=len(chunk),
        total_bytes=total_bytes,
        next_offset=next_offset if has_more else None,
        has_more=has_more,
        content_base64=base64.b64encode(chunk).decode("ascii"),
        sha256=expected_sha256,
        media_type=_media_type_for_kind(artifact_kind),
    )


SEQUENCE_COMPLETION_REPORT_RELATIVE = "reports/completion-v1.json"
SEQUENCE_COMPLETION_REPORT_MAX_BYTES = 131_072
INTEGRITY_SCHEMA_VALIDATED = "schema_validated"
INTEGRITY_LEDGER_SHA256_BOUND = "ledger_sha256_bound"


@dataclass(frozen=True)
class ValidatedPublishedSequenceReport:
    raw_bytes: bytes
    sha256: str
    integrity: str


def validate_published_sequence_report_bytes(
    raw: bytes,
    *,
    sequence_id: str,
    expected_sha256: str | None = None,
) -> ValidatedPublishedSequenceReport:
    if len(raw) > SEQUENCE_COMPLETION_REPORT_MAX_BYTES:
        raise FrozenArtifactReadError("artifact exceeds size limit")
    try:
        payload = json.loads(raw)
        report = SEQUENCE_COMPLETION_REPORT_ADAPTER.validate_python(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise FrozenArtifactReadError("published report schema invalid") from exc
    if report.sequence_id != sequence_id:
        raise FrozenArtifactReadError("published report identity mismatch")
    file_sha = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None:
        if file_sha != expected_sha256:
            raise FrozenArtifactReadError("published report hash mismatch")
        integrity = INTEGRITY_LEDGER_SHA256_BOUND
    else:
        integrity = INTEGRITY_SCHEMA_VALIDATED
    return ValidatedPublishedSequenceReport(
        raw_bytes=raw,
        sha256=file_sha,
        integrity=integrity,
    )


def load_integration_sequence_completion_report(
    *,
    artifact_root: Path,
    sequence_id: str,
    expected_sha256: str | None,
) -> ValidatedPublishedSequenceReport:
    try:
        return load_validated_published_sequence_report(
            artifact_root=artifact_root,
            sequence_id=sequence_id,
            expected_sha256=expected_sha256,
        )
    except FrozenArtifactReadError as exc:
        if "artifact missing" in str(exc):
            raise SequenceCompletionReportMissingError(str(exc)) from exc
        raise


def load_validated_published_sequence_report(
    *,
    artifact_root: Path,
    sequence_id: str,
    expected_sha256: str | None = None,
) -> ValidatedPublishedSequenceReport:
    path = _resolve_existing_sequence_artifact_path(
        artifact_root,
        sequence_id,
        SEQUENCE_COMPLETION_REPORT_RELATIVE,
    )
    raw = path.read_bytes()
    return validate_published_sequence_report_bytes(
        raw,
        sequence_id=sequence_id,
        expected_sha256=expected_sha256,
    )


@dataclass(frozen=True)
class VerifiedSequenceReportChunk:
    sequence_id: str
    byte_offset: int
    returned_bytes: int
    total_bytes: int
    next_offset: int | None
    has_more: bool
    content_base64: str
    sha256: str
    integrity: str


def chunk_validated_sequence_report(
    validated: ValidatedPublishedSequenceReport,
    *,
    sequence_id: str,
    byte_offset: int,
    limit: int,
) -> VerifiedSequenceReportChunk:
    validate_artifact_read_bounds(byte_offset, limit)
    total = len(validated.raw_bytes)
    if byte_offset > total:
        raise IntegrationApiError.invalid_argument("byteOffset is beyond the artifact size.")
    if byte_offset == total:
        return VerifiedSequenceReportChunk(
            sequence_id=sequence_id,
            byte_offset=byte_offset,
            returned_bytes=0,
            total_bytes=total,
            next_offset=None,
            has_more=False,
            content_base64="",
            sha256=validated.sha256,
            integrity=validated.integrity,
        )
    read_limit = min(limit, ARTIFACT_HARD_MAX_LIMIT, total - byte_offset)
    chunk = validated.raw_bytes[byte_offset : byte_offset + read_limit]
    next_offset = byte_offset + len(chunk)
    has_more = next_offset < total
    return VerifiedSequenceReportChunk(
        sequence_id=sequence_id,
        byte_offset=byte_offset,
        returned_bytes=len(chunk),
        total_bytes=total,
        next_offset=next_offset if has_more else None,
        has_more=has_more,
        content_base64=base64.b64encode(chunk).decode("ascii"),
        sha256=validated.sha256,
        integrity=validated.integrity,
    )


def read_published_sequence_report_chunk(
    *,
    artifact_root: Path,
    sequence_id: str,
    byte_offset: int,
    limit: int = 65536,
    expected_sha256: str | None = None,
) -> VerifiedSequenceReportChunk:
    validated = load_validated_published_sequence_report(
        artifact_root=artifact_root,
        sequence_id=sequence_id,
        expected_sha256=expected_sha256,
    )
    return chunk_validated_sequence_report(
        validated,
        sequence_id=sequence_id,
        byte_offset=byte_offset,
        limit=limit,
    )


def map_frozen_artifact_error(exc: Exception) -> IntegrationApiError:
    if isinstance(exc, IntegrationApiError):
        return exc
    if isinstance(exc, OSError):
        return IntegrationApiError.io_error("Unable to read the frozen artifact.")
    if isinstance(exc, FrozenArtifactReadError):
        message = str(exc)
        if "published report" in message:
            if "hash mismatch" in message:
                return IntegrationApiError.data_integrity(
                    "Published sequence report digest does not match ledger binding.",
                )
            if "identity mismatch" in message:
                return IntegrationApiError.data_integrity(
                    "Published sequence report identity does not match.",
                )
            if "schema invalid" in message:
                return IntegrationApiError.data_integrity("Published sequence report is invalid.")
            return IntegrationApiError.data_integrity("Published sequence report is invalid.")
        if "hash mismatch" in message:
            return IntegrationApiError.data_integrity("Frozen artifact hash verification failed.")
        if "missing" in message or "invalid" in message:
            return IntegrationApiError.data_integrity("Frozen artifact is missing or invalid.")
        if (
            "symlink" in message
            or "escapes" in message
            or "permissions" in message
            or "confined" in message
        ):
            return IntegrationApiError.data_integrity("Frozen artifact path is not safe to read.")
        if "path" in message:
            return IntegrationApiError.data_integrity("Frozen artifact path is not safe to read.")
        return IntegrationApiError.data_integrity("Frozen artifact could not be verified.")
    return IntegrationApiError.internal()
