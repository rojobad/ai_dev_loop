"""Authenticated Codex review attempt evidence for integration inspection."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.integration_api.review_artifact_io import (
    allowlisted_review_relative_path,
    expected_codex_review_result_rel,
    verify_confined_artifact,
)
from ai_dev_loop.review_result import CodexReviewResult
from ai_dev_loop.scheduler.application.attempt_envelope import (
    attempt_result_rel,
    attempt_stderr_rel,
    attempt_stdout_rel,
    read_bounded_bytes,
    validate_completion_evidence,
)
from ai_dev_loop.scheduler.application.codex_evidence import (
    CodexEvidenceError,
    verify_codex_invocation_evidence,
)
from ai_dev_loop.scheduler.application.codex_review_prompt_evidence import reviewer_session_ref
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    MAX_CODEX_REVIEW_RESULT_BYTES,
    RESUME_CODEX_REVIEW_EFFECT_KIND,
    codex_review_prompt_evidence_rel,
    codex_review_prompt_rel,
)
from ai_dev_loop.scheduler.domain.review_prompt_evidence import SchedulerReviewPromptEvidenceV1
from ai_dev_loop.scheduler.infrastructure.paths import resolve_run_relative_path

PromptEvidenceState = Literal[
    "available",
    "not_recorded",
    "not_yet_produced",
    "incomplete_capture",
    "data_integrity",
]
ResultState = Literal["valid", "invalid", "not_produced", "pending", "data_integrity"]


class ReviewEvidenceIntegrityError(Exception):
    """Trusted review evidence failed verification."""


@dataclass(frozen=True)
class AuthenticatedReviewEvidence:
    invocation: dict[str, object]
    outcome: dict[str, object] | None
    validated: CodexReviewResult | None
    result_state: ResultState
    prompt_state: PromptEvidenceState
    prompt_rel: str | None
    prompt_sha256: str | None
    prompt_size_bytes: int | None
    reviewer_session_id: str | None


def _prompt_evidence_state(
    run_root: Path,
    *,
    run_id: str,
    attempt_id: str,
    iteration: int,
    attempt_status: str,
) -> tuple[PromptEvidenceState, str | None, str | None, int | None]:
    prompt_rel = codex_review_prompt_rel(iteration, attempt_id)
    evidence_rel = codex_review_prompt_evidence_rel(iteration, attempt_id)
    if not allowlisted_review_relative_path(
        prompt_rel, review_iteration=iteration, attempt_id=attempt_id
    ):
        return "data_integrity", None, None, None
    if not allowlisted_review_relative_path(
        evidence_rel, review_iteration=iteration, attempt_id=attempt_id
    ):
        return "data_integrity", None, None, None
    try:
        prompt_path = resolve_run_relative_path(run_root, prompt_rel)
        evidence_path = resolve_run_relative_path(run_root, evidence_rel)
    except ValueError:
        return "data_integrity", None, None, None
    prompt_exists = prompt_path.is_file() and not prompt_path.is_symlink()
    evidence_exists = evidence_path.is_file() and not evidence_path.is_symlink()
    if prompt_exists and evidence_exists:
        try:
            evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence = SchedulerReviewPromptEvidenceV1.model_validate(evidence_payload)
        except (OSError, json.JSONDecodeError, PydanticValidationError):
            return "data_integrity", None, None, None
        if evidence.run_id != run_id or evidence.attempt_id != attempt_id:
            return "data_integrity", None, None, None
        if evidence.review_iteration != iteration or evidence.prompt_path != prompt_rel:
            return "data_integrity", None, None, None
        try:
            verify_confined_artifact(
                run_root,
                prompt_rel,
                expected_sha256=evidence.prompt_sha256,
                expected_size=evidence.prompt_size_bytes,
            )
        except Exception:
            return "data_integrity", None, None, None
        return (
            "available",
            prompt_rel,
            evidence.prompt_sha256,
            evidence.prompt_size_bytes,
        )
    if evidence_exists and not prompt_exists:
        return "data_integrity", None, None, None
    if prompt_exists and not evidence_exists:
        return "incomplete_capture", None, None, None
    if attempt_status in {"launching", "active"}:
        return "not_yet_produced", None, None, None
    return "not_recorded", None, None, None


def _load_authenticated_outcome(
    run_root: Path,
    *,
    attempt_id: str,
    unit_identity: str,
    dispatch_id: str,
    effect_kind: str,
    run_id: str,
    review_iteration: int,
    result_rel: str,
    stdout_rel: str,
    stderr_rel: str,
    completion_envelope_sha256: str | None,
) -> tuple[dict[str, object] | None, CodexReviewResult | None, ResultState]:
    if result_rel != attempt_result_rel(attempt_id):
        raise ReviewEvidenceIntegrityError("result artifact path mismatch")
    if stdout_rel != attempt_stdout_rel(attempt_id):
        raise ReviewEvidenceIntegrityError("stdout artifact path mismatch")
    if stderr_rel != attempt_stderr_rel(attempt_id):
        raise ReviewEvidenceIntegrityError("stderr artifact path mismatch")
    try:
        validated_completion = validate_completion_evidence(
            run_root=run_root,
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            result_rel=result_rel,
            stdout_rel=stdout_rel,
            stderr_rel=stderr_rel,
            observed_exit_code=None,
            observed_termination=None,
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ReviewEvidenceIntegrityError("completion envelope invalid") from exc
    if completion_envelope_sha256 and validated_completion.envelope_sha256 != (
        completion_envelope_sha256.strip()
    ):
        raise ReviewEvidenceIntegrityError("completion envelope digest mismatch")
    stdout_path = resolve_run_relative_path(run_root, stdout_rel)
    try:
        raw = read_bounded_bytes(stdout_path, MAX_CODEX_REVIEW_RESULT_BYTES + 1_048_576)
        outcome = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise ReviewEvidenceIntegrityError("authenticated stdout invalid") from exc
    if not isinstance(outcome, dict):
        raise ReviewEvidenceIntegrityError("authenticated stdout invalid")
    if str(outcome.get("attempt_id", "")) != attempt_id:
        raise ReviewEvidenceIntegrityError("stdout attempt_id mismatch")
    if str(outcome.get("dispatch_id", "")) != dispatch_id:
        raise ReviewEvidenceIntegrityError("stdout dispatch_id mismatch")
    if str(outcome.get("effect_kind", "")) != effect_kind:
        raise ReviewEvidenceIntegrityError("stdout effect_kind mismatch")
    if str(outcome.get("run_id", "")) != run_id:
        raise ReviewEvidenceIntegrityError("stdout run_id mismatch")
    result_path = str(outcome.get("review_result_path", "")).strip()
    result_sha = str(outcome.get("review_result_sha256", "")).strip()
    if not result_path and not result_sha:
        return outcome, None, "not_produced"
    expected_result = expected_codex_review_result_rel(review_iteration, attempt_id)
    if result_path != expected_result:
        raise ReviewEvidenceIntegrityError("review result path not allowlisted")
    if not allowlisted_review_relative_path(
        result_path, review_iteration=review_iteration, attempt_id=attempt_id
    ):
        raise ReviewEvidenceIntegrityError("review result path not allowlisted")
    if not result_sha:
        return outcome, None, "not_produced"
    try:
        verify_confined_artifact(
            run_root,
            result_path,
            expected_sha256=result_sha,
            max_bytes=MAX_CODEX_REVIEW_RESULT_BYTES,
        )
    except Exception as exc:
        raise ReviewEvidenceIntegrityError("review result verification failed") from exc
    try:
        path = resolve_run_relative_path(run_root, result_path)
        raw_result = read_bounded_bytes(path, MAX_CODEX_REVIEW_RESULT_BYTES)
        payload = json.loads(raw_result.decode("utf-8"))
        validated = CodexReviewResult.model_validate(payload)
    except (OSError, json.JSONDecodeError, PydanticValidationError, UnicodeError):
        return outcome, None, "invalid"
    return outcome, validated, "valid"


def load_authenticated_review_evidence(
    run_root: Path,
    attempt_row: sqlite3.Row,
    *,
    effect_kind: str,
) -> AuthenticatedReviewEvidence:
    attempt_id = str(attempt_row["attempt_id"])
    run_id = str(attempt_row["run_id"])
    dispatch_id = str(attempt_row["dispatch_id"])
    unit_identity = str(attempt_row["unit_identity"] or "")
    launch_intent_sha256 = str(attempt_row["launch_intent_sha256"] or "")
    launch_nonce = str(attempt_row["launch_nonce"] or "")
    status = str(attempt_row["status"])
    iteration = int(attempt_row["iteration"])
    completion_sha = attempt_row["completion_envelope_sha256"]
    completion_envelope_sha256 = str(completion_sha).strip() if completion_sha is not None else None
    if not unit_identity or not launch_intent_sha256 or not launch_nonce:
        raise ReviewEvidenceIntegrityError("attempt launch binding incomplete")
    try:
        invocation = verify_codex_invocation_evidence(
            run_root,
            attempt_id=attempt_id,
            run_id=run_id,
            dispatch_id=dispatch_id,
            unit_identity=unit_identity,
            launch_nonce=launch_nonce,
            launch_intent_sha256=launch_intent_sha256,
            effect_kind=effect_kind,
        )
    except CodexEvidenceError as exc:
        raise ReviewEvidenceIntegrityError(str(exc)) from exc
    raw_iteration = invocation.get("review_iteration", iteration)
    if isinstance(raw_iteration, bool) or not isinstance(raw_iteration, int):
        review_iteration = iteration
    else:
        review_iteration = raw_iteration
    prompt_state, prompt_rel, prompt_sha, prompt_size = _prompt_evidence_state(
        run_root,
        run_id=run_id,
        attempt_id=attempt_id,
        iteration=review_iteration,
        attempt_status=status,
    )
    if prompt_state == "data_integrity":
        raise ReviewEvidenceIntegrityError("prompt evidence integrity failure")
    outcome: dict[str, object] | None = None
    validated: CodexReviewResult | None = None
    result_state: ResultState = "pending"
    if status in {"launching", "active"}:
        result_state = "pending"
    elif not completion_envelope_sha256:
        if status in {"uncertain", "cancelled"}:
            result_state = "pending" if status == "uncertain" else "not_produced"
        elif status in {"completed", "failed"}:
            raise ReviewEvidenceIntegrityError("completion binding missing from ledger")
        else:
            result_state = "pending"
    else:
        outcome, validated, result_state = _load_authenticated_outcome(
            run_root,
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            dispatch_id=dispatch_id,
            effect_kind=effect_kind,
            run_id=run_id,
            review_iteration=review_iteration,
            result_rel=str(attempt_row["result_artifact_path"]),
            stdout_rel=str(attempt_row["stdout_artifact_path"]),
            stderr_rel=str(attempt_row["stderr_artifact_path"]),
            completion_envelope_sha256=completion_envelope_sha256,
        )
    reviewer_session_id: str | None = None
    if effect_kind == RESUME_CODEX_REVIEW_EFFECT_KIND:
        reviewer_session_id = str(invocation.get("reviewer_session_id", "")).strip() or None
    elif effect_kind == BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND and outcome is not None:
        reviewer_session_id = str(outcome.get("bootstrap_session_id", "")).strip() or None
    return AuthenticatedReviewEvidence(
        invocation=invocation,
        outcome=outcome,
        validated=validated,
        result_state=result_state,
        prompt_state=prompt_state,
        prompt_rel=prompt_rel,
        prompt_sha256=prompt_sha,
        prompt_size_bytes=prompt_size,
        reviewer_session_id=reviewer_session_id,
    )


def reviewer_ref_from_auth(auth: AuthenticatedReviewEvidence) -> str | None:
    return reviewer_session_ref(auth.reviewer_session_id)
