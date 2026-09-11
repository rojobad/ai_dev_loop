"""Codex attempt evidence authentication and outcome validation."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.fresh_codex_reviewer import classify_bootstrap_session_id_from_text
from ai_dev_loop.review_result import CodexReviewResult
from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.attempt_envelope import (
    read_bounded_bytes,
    sha256_file,
    validate_completion_evidence,
)
from ai_dev_loop.scheduler.application.cursor_evidence import (
    CursorEvidenceError,
    invocation_evidence_sha256,
    verify_prompt_binding,
)
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    CODEX_ATTEMPT_EFFECT_KINDS,
    MAX_CODEX_EVENTS_ARTIFACT_BYTES,
    MAX_CODEX_REVIEW_RESULT_BYTES,
    RESUME_CODEX_REVIEW_EFFECT_KIND,
    SCHEDULER_CODEX_REVIEW_SANDBOX,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import invocation_evidence_rel


class CodexEvidenceError(CursorEvidenceError):
    """Codex-specific evidence validation failure."""


def authenticate_pinned_codex_invocation_evidence(
    run_root: Path,
    *,
    attempt_id: str,
    pinned_invocation_evidence_sha256: str,
    run_id: str,
    dispatch_id: str,
    unit_identity: str,
    launch_nonce: str,
    launch_intent_sha256: str,
    effect_kind: str,
) -> dict[str, object]:
    evidence_path = run_root / invocation_evidence_rel(attempt_id)
    if not evidence_path.is_file():
        raise CodexEvidenceError("invocation evidence artifact missing")
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexEvidenceError("invocation evidence is not valid JSON") from exc
    if not isinstance(evidence, dict):
        raise CodexEvidenceError("invocation evidence must be a JSON object")
    if invocation_evidence_sha256(evidence) != pinned_invocation_evidence_sha256:
        raise CodexEvidenceError("pinned invocation evidence digest mismatch")
    verified = verify_codex_invocation_evidence(
        run_root,
        attempt_id=attempt_id,
        run_id=run_id,
        dispatch_id=dispatch_id,
        unit_identity=unit_identity,
        launch_nonce=launch_nonce,
        launch_intent_sha256=launch_intent_sha256,
        effect_kind=effect_kind,
    )
    verify_pre_execution_codex_guards(run_root, verified, run_id=run_id)
    return verified


def verify_codex_invocation_evidence(
    run_root: Path,
    *,
    attempt_id: str,
    run_id: str,
    dispatch_id: str,
    unit_identity: str,
    launch_nonce: str,
    launch_intent_sha256: str,
    effect_kind: str,
) -> dict[str, object]:
    if effect_kind not in CODEX_ATTEMPT_EFFECT_KINDS:
        raise CodexEvidenceError(f"unsupported codex effect kind: {effect_kind}")
    evidence_path = run_root / invocation_evidence_rel(attempt_id)
    if not evidence_path.is_file():
        raise CodexEvidenceError("codex invocation evidence is missing")
    evidence_bytes = evidence_path.read_bytes()
    try:
        payload = json.loads(evidence_bytes.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexEvidenceError("codex invocation evidence is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CodexEvidenceError("codex invocation evidence must be a JSON object")
    evidence_hash = invocation_evidence_sha256(payload)
    launch_intent = json.dumps(
        {
            "attempt_id": attempt_id,
            "dispatch_id": dispatch_id,
            "run_id": run_id,
            "unit_identity": unit_identity,
            "launch_nonce": launch_nonce,
            "effect_kind": effect_kind,
            "invocation_evidence_sha256": evidence_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if payload_sha256(launch_intent) != launch_intent_sha256:
        raise CodexEvidenceError("codex launch intent does not match invocation evidence")
    if str(payload.get("effect_kind", "")) != effect_kind:
        raise CodexEvidenceError("codex invocation evidence effect_kind mismatch")
    if str(payload.get("run_id", "")) != run_id:
        raise CodexEvidenceError("codex invocation evidence run_id mismatch")
    if str(payload.get("dispatch_id", "")) != dispatch_id:
        raise CodexEvidenceError("codex invocation evidence dispatch_id mismatch")
    if str(payload.get("attempt_id", "")) != attempt_id:
        raise CodexEvidenceError("codex invocation evidence attempt_id mismatch")
    return payload


def _verify_frozen_plan_prompt_artifacts(
    run_root: Path,
    evidence: dict[str, object],
) -> None:
    plan_path = str(evidence.get("plan_artifact_path", "")).strip()
    plan_sha = str(evidence.get("plan_sha256", "")).strip()
    if not plan_path or not plan_sha:
        raise CodexEvidenceError("codex review missing frozen plan artifact binding")
    verify_prompt_binding(run_root, prompt_path=plan_path, prompt_sha256=plan_sha)
    prompt_path = str(evidence.get("prompt_artifact_path", "")).strip()
    prompt_sha = str(evidence.get("prompt_sha256", "")).strip()
    if not prompt_path or not prompt_sha:
        raise CodexEvidenceError("codex review missing frozen prompt artifact binding")
    verify_prompt_binding(run_root, prompt_path=prompt_path, prompt_sha256=prompt_sha)


def verify_pre_execution_codex_guards(
    run_root: Path,
    evidence: dict[str, object],
    *,
    run_id: str,
) -> None:
    if str(evidence.get("run_id", "")) != run_id:
        raise CodexEvidenceError("codex invocation evidence run_id mismatch")
    sandbox = str(evidence.get("codex_sandbox", "")).strip()
    if sandbox != SCHEDULER_CODEX_REVIEW_SANDBOX:
        raise CodexEvidenceError("scheduler codex review requires read-only sandbox")
    _verify_frozen_plan_prompt_artifacts(run_root, evidence)
    if effect_kind := str(evidence.get("effect_kind", "")):
        if effect_kind == RESUME_CODEX_REVIEW_EFFECT_KIND:
            session_id = str(evidence.get("reviewer_session_id", "")).strip()
            if not session_id:
                raise CodexEvidenceError("codex resume requires bound reviewer session id")
        elif effect_kind == BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND:
            if str(evidence.get("reviewer_session_id", "")).strip():
                raise CodexEvidenceError("codex bootstrap cannot run with bound reviewer identity")


def load_authenticated_codex_outcome(
    run_root: Path,
    *,
    attempt_id: str,
    unit_identity: str,
    result_rel: str,
    stdout_rel: str,
    stderr_rel: str,
    observed_exit_code: int | None,
    expected_envelope_sha256: str | None,
    expected_dispatch_id: str | None,
    expected_effect_kind: str | None,
) -> dict[str, object]:
    validated = validate_completion_evidence(
        run_root=run_root,
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        result_rel=result_rel,
        stdout_rel=stdout_rel,
        stderr_rel=stderr_rel,
        observed_exit_code=observed_exit_code,
        observed_termination=None,
    )
    if expected_envelope_sha256 and validated.envelope_sha256 != expected_envelope_sha256:
        raise CodexEvidenceError("completion envelope hash does not match attempt record")
    stdout_path = run_root / stdout_rel
    try:
        outcome = json.loads(stdout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexEvidenceError("codex attempt stdout is not valid JSON") from exc
    if not isinstance(outcome, dict):
        raise CodexEvidenceError("codex attempt stdout must be a JSON object")
    if expected_dispatch_id and str(outcome.get("dispatch_id", "")) != expected_dispatch_id:
        raise CodexEvidenceError("codex stdout dispatch_id mismatch")
    if expected_effect_kind and str(outcome.get("effect_kind", "")) != expected_effect_kind:
        raise CodexEvidenceError("codex stdout effect_kind mismatch")
    if str(outcome.get("attempt_id", "")) != attempt_id:
        raise CodexEvidenceError("codex stdout attempt_id mismatch")
    _ = TerminationClass
    return outcome


def validate_codex_review_outcome_semantics(
    outcome: dict[str, object],
    *,
    expected_review_iteration: int,
    expected_effect_kind: str,
    bound_session_id: str | None,
) -> None:
    effect_kind = str(outcome.get("effect_kind", ""))
    if effect_kind != expected_effect_kind:
        raise CodexEvidenceError("codex outcome effect_kind disagrees with dispatch")
    iteration = outcome.get("review_iteration")
    if not isinstance(iteration, int) or iteration != expected_review_iteration:
        raise CodexEvidenceError("codex outcome review_iteration mismatch")
    if effect_kind == BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND:
        uncertainty = outcome.get("bootstrap_uncertainty_reason")
        if uncertainty:
            raise CodexEvidenceError(f"codex bootstrap uncertainty: {uncertainty}")
        session_id = str(outcome.get("bootstrap_session_id", "")).strip()
        if not session_id:
            raise CodexEvidenceError("codex bootstrap outcome missing reviewer identity")
        if bound_session_id and bound_session_id != session_id:
            raise CodexEvidenceError("codex bootstrap identity conflicts with bound reviewer")
    elif effect_kind == RESUME_CODEX_REVIEW_EFFECT_KIND:
        session_id = str(outcome.get("resume_session_id", "")).strip()
        if not bound_session_id or session_id != bound_session_id:
            raise CodexEvidenceError("codex resume outcome session mismatch")
    else:
        raise CodexEvidenceError("unsupported codex outcome effect kind")
    result_path = str(outcome.get("review_result_path", "")).strip()
    result_sha = str(outcome.get("review_result_sha256", "")).strip()
    if not result_path or not result_sha:
        raise CodexEvidenceError("codex outcome missing review result binding")
    if outcome.get("timed_out"):
        raise CodexEvidenceError("codex review timed out")
    returncode = outcome.get("returncode", 0)
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise CodexEvidenceError("codex outcome returncode invalid")
    if returncode != 0:
        raise CodexEvidenceError("codex review exited with failure")


def _read_bounded_json_artifact(
    path: Path,
    *,
    max_bytes: int,
    missing_message: str,
    overflow_message: str,
) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise CodexEvidenceError(missing_message)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CodexEvidenceError(missing_message) from exc
    if size > max_bytes:
        raise CodexEvidenceError(overflow_message)
    raw = read_bounded_bytes(path, max_bytes)
    if len(raw) > max_bytes:
        raise CodexEvidenceError(overflow_message)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CodexEvidenceError("codex review result is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CodexEvidenceError("codex review result must be a JSON object")
    return payload


def load_validated_review_result(run_root: Path, outcome: dict[str, object]) -> CodexReviewResult:
    result_path = run_root / str(outcome["review_result_path"])
    expected_sha = str(outcome["review_result_sha256"])
    actual_sha = sha256_file(result_path)
    if actual_sha != expected_sha:
        raise CodexEvidenceError("codex review result digest mismatch")
    payload = _read_bounded_json_artifact(
        result_path,
        max_bytes=MAX_CODEX_REVIEW_RESULT_BYTES,
        missing_message="codex review result artifact missing",
        overflow_message="codex review result exceeds size bound",
    )
    try:
        return CodexReviewResult.model_validate(payload)
    except PydanticValidationError as exc:
        raise CodexEvidenceError("codex review result failed schema validation") from exc


def classify_bootstrap_identity_from_outcome(
    run_root: Path,
    outcome: dict[str, object],
) -> tuple[str | None, str | None]:
    events_rel = str(outcome.get("events_path", "")).strip()
    if not events_rel:
        return None, "missing_identity"
    events_path = run_root / events_rel
    if not events_path.is_file() or events_path.is_symlink():
        return None, "missing_identity"
    try:
        size = events_path.stat().st_size
    except OSError:
        return None, "missing_identity"
    if size > MAX_CODEX_EVENTS_ARTIFACT_BYTES:
        return None, "missing_identity"
    try:
        text = read_bounded_bytes(events_path, MAX_CODEX_EVENTS_ARTIFACT_BYTES).decode("utf-8")
    except (OSError, UnicodeError):
        return None, "missing_identity"
    capture = classify_bootstrap_session_id_from_text(text)
    return capture.session_id, capture.uncertainty_reason
