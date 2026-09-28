"""Confined read-only IO for per-attempt Codex review inspection artifacts."""

from __future__ import annotations

import base64
import re
from pathlib import Path

from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.scheduler.application.artifact_digest import read_file_chunk, verify_file_digest
from ai_dev_loop.scheduler.domain.codex_contract import (
    codex_review_prompt_evidence_rel,
    codex_review_prompt_rel,
    codex_review_result_rel,
)
from ai_dev_loop.scheduler.infrastructure.paths import resolve_run_relative_path

_CODEX_REVIEW_RESULT_RE = re.compile(r"^codex/reviews/\d{2}\.[A-Za-z0-9._-]+\.json$")


def expected_codex_review_result_rel(review_iteration: int, attempt_id: str) -> str:
    return codex_review_result_rel(review_iteration, attempt_id)


def allowlisted_review_relative_path(
    relative_path: str,
    *,
    review_iteration: int,
    attempt_id: str,
) -> bool:
    if relative_path == codex_review_prompt_rel(review_iteration, attempt_id):
        return True
    if relative_path == codex_review_prompt_evidence_rel(review_iteration, attempt_id):
        return True
    if relative_path == expected_codex_review_result_rel(review_iteration, attempt_id):
        return True
    return bool(_CODEX_REVIEW_RESULT_RE.match(relative_path))


def resolve_allowlisted_artifact(run_root: Path, relative_path: str) -> Path:
    try:
        path = resolve_run_relative_path(run_root, relative_path)
    except ValueError as exc:
        raise IntegrationApiError.data_integrity("Artifact path is not allowlisted.") from exc
    if not path.is_file() or path.is_symlink():
        raise IntegrationApiError.data_integrity("Artifact missing.")
    try:
        path.resolve(strict=False).relative_to(run_root.resolve(strict=False))
    except ValueError as exc:
        raise IntegrationApiError.data_integrity("Artifact path escapes run root.") from exc
    return path


def verify_confined_artifact(
    run_root: Path,
    relative_path: str,
    *,
    expected_sha256: str,
    expected_size: int | None = None,
    max_bytes: int | None = None,
) -> int:
    path = resolve_allowlisted_artifact(run_root, relative_path)
    if max_bytes is not None:
        size = path.stat().st_size
        if size > max_bytes:
            raise IntegrationApiError.data_integrity("Artifact exceeds size bound.")
    try:
        return verify_file_digest(
            path,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
    except ValueError as exc:
        raise IntegrationApiError.data_integrity("Artifact verification failed.") from exc


def read_confined_chunk(
    run_root: Path,
    relative_path: str,
    *,
    expected_sha256: str,
    expected_size: int | None,
    byte_offset: int,
    limit: int,
    max_bytes: int | None = None,
) -> tuple[bytes, int]:
    total = verify_confined_artifact(
        run_root,
        relative_path,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        max_bytes=max_bytes,
    )
    path = resolve_allowlisted_artifact(run_root, relative_path)
    try:
        chunk = read_file_chunk(path, byte_offset=byte_offset, limit=limit, total_bytes=total)
    except ValueError as exc:
        raise IntegrationApiError.invalid_argument(str(exc)) from exc
    return chunk, total


def chunk_to_base64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")
