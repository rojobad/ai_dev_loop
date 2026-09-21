"""Read-only per-attempt Codex review content chunks for the Integration API."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.review_artifact_io import (
    chunk_to_base64,
    expected_codex_review_result_rel,
    read_confined_chunk,
)
from ai_dev_loop.integration_api.review_evidence_auth import AuthenticatedReviewEvidence
from ai_dev_loop.integration_api.review_projection import (
    build_review_detail_data,
    confined_run_root,
)
from ai_dev_loop.integration_api.validation import validate_artifact_read_bounds
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    MAX_CODEX_REVIEW_RESULT_BYTES,
    RESUME_CODEX_REVIEW_EFFECT_KIND,
)

ReviewContentKind = Literal["prompt", "response", "review-markdown", "cursor-fix-prompt"]


@dataclass(frozen=True)
class ReviewContentChunk:
    run_id: str
    attempt_id: str
    content_kind: str
    byte_offset: int
    returned_bytes: int
    total_bytes: int
    next_offset: int | None
    has_more: bool
    content_base64: str
    sha256: str | None
    available: bool
    reason: str | None


def _chunk_from_slice(
    *,
    run_id: str,
    attempt_id: str,
    content_kind: str,
    slice_data: bytes,
    byte_offset: int,
    total_bytes: int,
    full_digest: str,
) -> ReviewContentChunk:
    end = byte_offset + len(slice_data)
    has_more = end < total_bytes
    next_offset = end if has_more else None
    return ReviewContentChunk(
        run_id=run_id,
        attempt_id=attempt_id,
        content_kind=content_kind,
        byte_offset=byte_offset,
        returned_bytes=len(slice_data),
        total_bytes=total_bytes,
        next_offset=next_offset,
        has_more=has_more,
        content_base64=chunk_to_base64(slice_data),
        sha256=full_digest,
        available=True,
        reason=None,
    )


def _unavailable_chunk(
    *,
    run_id: str,
    attempt_id: str,
    content_kind: str,
    reason: str,
) -> ReviewContentChunk:
    return ReviewContentChunk(
        run_id=run_id,
        attempt_id=attempt_id,
        content_kind=content_kind,
        byte_offset=0,
        returned_bytes=0,
        total_bytes=0,
        next_offset=None,
        has_more=False,
        content_base64="",
        sha256=None,
        available=False,
        reason=reason,
    )


def read_review_content_chunk(
    *,
    artifact_root: Path,
    run_id: str,
    attempt_row: sqlite3.Row,
    effect_kind: str,
    auth: AuthenticatedReviewEvidence,
    content_kind: ReviewContentKind,
    byte_offset: int,
    limit: int,
) -> ReviewContentChunk:
    validate_artifact_read_bounds(byte_offset, limit)
    run_root = confined_run_root(artifact_root, run_id)
    attempt_id = str(attempt_row["attempt_id"])
    detail = build_review_detail_data(
        run_id=run_id,
        attempt_row=attempt_row,
        effect_kind=effect_kind,
        auth=auth,
    )

    if content_kind == "prompt":
        availability = detail.content.prompt
        if not availability.available:
            if availability.reason == "data_integrity":
                raise IntegrationApiError.data_integrity("Prompt evidence failed verification.")
            return _unavailable_chunk(
                run_id=run_id,
                attempt_id=attempt_id,
                content_kind=content_kind,
                reason=availability.reason or "unavailable",
            )
        if not auth.prompt_rel or not auth.prompt_sha256 or auth.prompt_size_bytes is None:
            raise IntegrationApiError.data_integrity("Prompt evidence failed verification.")
        slice_data, total = read_confined_chunk(
            run_root,
            auth.prompt_rel,
            expected_sha256=auth.prompt_sha256,
            expected_size=auth.prompt_size_bytes,
            byte_offset=byte_offset,
            limit=limit,
        )
        return _chunk_from_slice(
            run_id=run_id,
            attempt_id=attempt_id,
            content_kind=content_kind,
            slice_data=slice_data,
            byte_offset=byte_offset,
            total_bytes=total,
            full_digest=auth.prompt_sha256,
        )

    if content_kind == "response":
        availability = detail.content.response
        if not availability.available:
            return _unavailable_chunk(
                run_id=run_id,
                attempt_id=attempt_id,
                content_kind=content_kind,
                reason=availability.reason or "unavailable",
            )
        if auth.outcome is None:
            return _unavailable_chunk(
                run_id=run_id,
                attempt_id=attempt_id,
                content_kind=content_kind,
                reason="not_produced",
            )
        result_path = str(auth.outcome.get("review_result_path", "")).strip()
        result_sha = str(auth.outcome.get("review_result_sha256", "")).strip()
        if not result_path or not result_sha:
            return _unavailable_chunk(
                run_id=run_id,
                attempt_id=attempt_id,
                content_kind=content_kind,
                reason="not_produced",
            )
        expected = expected_codex_review_result_rel(int(attempt_row["iteration"]), attempt_id)
        if result_path != expected:
            raise IntegrationApiError.data_integrity("Review response binding mismatch.")
        slice_data, total = read_confined_chunk(
            run_root,
            result_path,
            expected_sha256=result_sha,
            expected_size=None,
            byte_offset=byte_offset,
            limit=limit,
            max_bytes=MAX_CODEX_REVIEW_RESULT_BYTES,
        )
        return _chunk_from_slice(
            run_id=run_id,
            attempt_id=attempt_id,
            content_kind=content_kind,
            slice_data=slice_data,
            byte_offset=byte_offset,
            total_bytes=total,
            full_digest=result_sha,
        )

    if content_kind == "review-markdown":
        availability = detail.content.review_markdown
        if not availability.available:
            if availability.reason == "data_integrity":
                raise IntegrationApiError.data_integrity("Review evidence failed verification.")
            return _unavailable_chunk(
                run_id=run_id,
                attempt_id=attempt_id,
                content_kind=content_kind,
                reason=availability.reason or "unavailable",
            )
        if auth.validated is None:
            return _unavailable_chunk(
                run_id=run_id,
                attempt_id=attempt_id,
                content_kind=content_kind,
                reason="invalid_result",
            )
        data = auth.validated.review_markdown.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        if byte_offset > len(data):
            raise IntegrationApiError.invalid_argument("byte offset beyond artifact size")
        end = min(byte_offset + limit, len(data))
        slice_data = data[byte_offset:end]
        return _chunk_from_slice(
            run_id=run_id,
            attempt_id=attempt_id,
            content_kind=content_kind,
            slice_data=slice_data,
            byte_offset=byte_offset,
            total_bytes=len(data),
            full_digest=digest,
        )

    if content_kind == "cursor-fix-prompt":
        availability = detail.content.cursor_fix_prompt
        if not availability.available:
            return _unavailable_chunk(
                run_id=run_id,
                attempt_id=attempt_id,
                content_kind=content_kind,
                reason=availability.reason or "unavailable",
            )
        if auth.validated is None or not auth.validated.cursor_fix_prompt:
            return _unavailable_chunk(
                run_id=run_id,
                attempt_id=attempt_id,
                content_kind=content_kind,
                reason="not_applicable",
            )
        data = auth.validated.cursor_fix_prompt.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        if byte_offset > len(data):
            raise IntegrationApiError.invalid_argument("byte offset beyond artifact size")
        end = min(byte_offset + limit, len(data))
        slice_data = data[byte_offset:end]
        return _chunk_from_slice(
            run_id=run_id,
            attempt_id=attempt_id,
            content_kind=content_kind,
            slice_data=slice_data,
            byte_offset=byte_offset,
            total_bytes=len(data),
            full_digest=digest,
        )

    raise IntegrationApiError.invalid_argument("Unsupported review content kind.")


def review_content_chunk_to_wire(chunk: ReviewContentChunk) -> dict[str, object]:
    if chunk.available:
        return {
            "runId": chunk.run_id,
            "attemptId": chunk.attempt_id,
            "contentKind": chunk.content_kind,
            "available": True,
            "reason": None,
            "encoding": "base64",
            "mediaType": "text/plain",
            "byteOffset": chunk.byte_offset,
            "returnedBytes": chunk.returned_bytes,
            "nextOffset": chunk.next_offset,
            "availableBytes": chunk.total_bytes,
            "hasMore": chunk.has_more,
            "contentBase64": chunk.content_base64,
            "sha256": chunk.sha256,
        }
    return {
        "runId": chunk.run_id,
        "attemptId": chunk.attempt_id,
        "contentKind": chunk.content_kind,
        "available": False,
        "reason": chunk.reason,
        "encoding": "base64",
        "mediaType": None,
        "byteOffset": 0,
        "returnedBytes": 0,
        "nextOffset": None,
        "availableBytes": None,
        "hasMore": False,
        "contentBase64": "",
        "sha256": None,
    }


def is_codex_review_effect(effect_kind: str | None) -> bool:
    return effect_kind in {
        BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        RESUME_CODEX_REVIEW_EFFECT_KIND,
    }


__all__ = [
    "ReviewContentKind",
    "is_codex_review_effect",
    "read_review_content_chunk",
    "review_content_chunk_to_wire",
]
