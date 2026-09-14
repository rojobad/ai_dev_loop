"""Artifact verification for exhausted-review budget extension recovery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.iterations import (
    build_correction_execution_envelope,
    correction_execution_envelope_path,
    extract_correction_fix_prompt,
    fix_prompt_path,
)
from ai_dev_loop.scheduler.application.attempt_envelope import read_bounded_bytes
from ai_dev_loop.scheduler.application.codex_evidence import (
    CodexEvidenceError,
    load_validated_review_result,
    validate_codex_review_outcome_integrity,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.review_recovery import _authenticate_codex_attempt
from ai_dev_loop.scheduler.domain.state import MaxIterationsReachedState
from ai_dev_loop.scheduler.infrastructure.paths import resolve_run_relative_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    MAX_PROMPT_BYTES,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import sha256_file


@dataclass(frozen=True)
class ExhaustedReviewArtifacts:
    review_iteration: int
    review_result_path: str
    review_result_sha256: str
    fix_prompt_path: str
    fix_prompt_sha256: str
    correction_envelope_path: str
    correction_envelope_sha256: str


def load_exhausted_review_artifacts(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    state: MaxIterationsReachedState,
) -> ExhaustedReviewArtifacts:
    review_iteration = state.codex.review_iteration
    if review_iteration != state.codex.reviews_completed:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review checkpoint disagrees with completed review count",
        )
    with store.begin_read() as conn:
        attempt = store.get_latest_recorded_codex_attempt(conn, state.run_id)
    if attempt is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "maxed run lacks a recorded Codex review attempt",
        )
    outcome, effect_kind = _authenticate_codex_attempt(
        store,
        artifacts,
        source_run_id=state.run_id,
        attempt=attempt,
    )
    with store.begin_read() as conn:
        dispatch = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
    if dispatch is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review attempt dispatch record missing",
        )
    payload = dispatch["effect_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review attempt dispatch payload invalid",
        )
    attempt_review_iteration = int(payload.get("review_iteration", 0))
    if attempt_review_iteration != review_iteration:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "latest Codex attempt does not match the exhausted review iteration",
        )
    run_root = artifacts.run_root(state.run_id)
    try:
        validate_codex_review_outcome_integrity(
            outcome,
            run_root=run_root,
            expected_review_iteration=review_iteration,
            expected_effect_kind=effect_kind,
            bound_session_id=state.codex.reviewer_session_id,
        )
        review = load_validated_review_result(run_root, outcome)
    except CodexEvidenceError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            f"exhausted review artifacts failed validation: {exc}",
        ) from exc
    if not review.has_actionable_findings:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review result does not contain actionable findings",
        )
    if not review.cursor_fix_prompt or not review.cursor_fix_prompt.strip():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review result is missing cursor_fix_prompt",
        )
    result_path = str(outcome.get("review_result_path", "")).strip()
    result_sha = str(outcome.get("review_result_sha256", "")).strip()
    fix_path = str(outcome.get("fix_prompt_path", "")).strip()
    fix_sha = str(outcome.get("fix_prompt_sha256", "")).strip()
    envelope_path = str(outcome.get("execution_envelope_path", "")).strip()
    envelope_sha = str(outcome.get("execution_envelope_sha256", "")).strip()
    if not result_path or not result_sha:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review outcome missing review result binding",
        )
    if not fix_path or not fix_sha:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review outcome missing fix prompt binding",
        )
    if not envelope_path or not envelope_sha:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review outcome missing correction envelope binding",
        )
    expected_fix_path = fix_prompt_path(review_iteration)
    expected_envelope_path = correction_execution_envelope_path(review_iteration)
    if fix_path != expected_fix_path:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review fix prompt path does not match review iteration",
        )
    if envelope_path != expected_envelope_path:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review correction envelope path does not match review iteration",
        )
    _validate_protected_relative_paths(
        run_root,
        result_path=result_path,
        fix_path=fix_path,
        envelope_path=envelope_path,
    )
    fix_prompt_bytes = review.cursor_fix_prompt.encode("utf-8")
    try:
        fix_artifact_bytes = _read_hash_verified_artifact_bytes(
            run_root,
            fix_path,
            expected_sha256=fix_sha,
            max_bytes=MAX_PROMPT_BYTES,
        )
        envelope_artifact_bytes = _read_hash_verified_artifact_bytes(
            run_root,
            envelope_path,
            expected_sha256=envelope_sha,
            max_bytes=MAX_PROMPT_BYTES,
        )
        _read_hash_verified_artifact_bytes(
            run_root,
            result_path,
            expected_sha256=result_sha,
            max_bytes=MAX_PROMPT_BYTES,
        )
    except SchedulerEngineError:
        raise
    except OSError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            f"exhausted review artifact verification failed: {exc}",
        ) from exc
    if not fix_artifact_bytes:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review fix prompt artifact is empty",
        )
    if fix_artifact_bytes != fix_prompt_bytes:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review fix prompt does not match schema-validated cursor_fix_prompt",
        )
    expected_envelope = build_correction_execution_envelope(review.cursor_fix_prompt)
    envelope_text = envelope_artifact_bytes.decode("utf-8")
    if envelope_text != expected_envelope:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review correction envelope does not embed the exact fix prompt",
        )
    extracted = extract_correction_fix_prompt(envelope_text)
    if extracted != review.cursor_fix_prompt:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review correction envelope body mismatch",
        )
    return ExhaustedReviewArtifacts(
        review_iteration=review_iteration,
        review_result_path=result_path,
        review_result_sha256=result_sha,
        fix_prompt_path=fix_path,
        fix_prompt_sha256=fix_sha,
        correction_envelope_path=envelope_path,
        correction_envelope_sha256=envelope_sha,
    )


def _read_hash_verified_artifact_bytes(
    run_root: Path,
    relative_path: str,
    *,
    expected_sha256: str,
    max_bytes: int,
) -> bytes:
    path = resolve_run_relative_path(run_root, relative_path)
    if path.is_symlink() or not path.is_file():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review artifact path does not resolve to a file",
        )
    content = read_bounded_bytes(path, max_bytes + 1)
    if not content:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review artifact is empty",
        )
    if len(content) > max_bytes:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review artifact exceeds size bound",
        )
    if sha256_file(path) != expected_sha256:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review artifact digest mismatch",
        )
    return content


def _validate_protected_relative_paths(
    run_root: Path,
    *,
    result_path: str,
    fix_path: str,
    envelope_path: str,
) -> None:
    for rel_path in (result_path, fix_path, envelope_path):
        try:
            resolved = resolve_run_relative_path(run_root, rel_path)
        except ValueError as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                f"exhausted review artifact path is not a safe relative path: {exc}",
            ) from exc
        if not resolved.is_file():
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "exhausted review artifact path does not resolve to a file",
            )
