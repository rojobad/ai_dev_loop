"""Publish and verify per-attempt Codex review stdin evidence before launch."""

from __future__ import annotations

import hashlib
import json
import os

from ai_dev_loop.scheduler.application.artifact_digest import verify_file_digest
from ai_dev_loop.scheduler.domain.codex_contract import (
    codex_review_prompt_evidence_rel,
    codex_review_prompt_rel,
)
from ai_dev_loop.scheduler.domain.review_prompt_evidence import SchedulerReviewPromptEvidenceV1
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
    StoredArtifact,
)

_FAULT_ENV = "AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT"


class CodexReviewPromptEvidenceError(Exception):
    """Failed to publish or verify review prompt evidence."""


def _maybe_inject_fault(point: str) -> None:
    fault = os.environ.get(_FAULT_ENV, "").strip()
    if fault == point:
        raise CodexReviewPromptEvidenceError(f"injected fault at {point}")


def _canonical_evidence_text(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def publish_review_prompt_before_launch(
    artifacts: ProtectedArtifactStore,
    *,
    run_id: str,
    attempt_id: str,
    review_iteration: int,
    prompt: str,
) -> tuple[StoredArtifact, StoredArtifact]:
    """Write prompt bytes and evidence JSON, verifying both before Codex launch."""

    prompt_bytes = prompt.encode("utf-8")
    if not prompt_bytes:
        raise CodexReviewPromptEvidenceError("review prompt is empty")
    max_bytes = len(prompt_bytes)
    prompt_rel = codex_review_prompt_rel(review_iteration, attempt_id)
    evidence_rel = codex_review_prompt_evidence_rel(review_iteration, attempt_id)

    _maybe_inject_fault("before_prompt")
    try:
        prompt_stored = artifacts.publish_or_verify_bytes(
            run_id,
            prompt_rel,
            prompt_bytes,
            max_bytes=max_bytes,
        )
    except ProtectedArtifactError as exc:
        raise CodexReviewPromptEvidenceError(str(exc)) from exc
    _maybe_inject_fault("after_prompt")

    evidence_payload = SchedulerReviewPromptEvidenceV1.model_validate(
        {
            "schema_version": 1,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "review_iteration": review_iteration,
            "prompt_path": prompt_rel,
            "prompt_sha256": prompt_stored.sha256,
            "prompt_size_bytes": prompt_stored.size_bytes,
        }
    )
    evidence_text = _canonical_evidence_text(evidence_payload.model_dump())
    _maybe_inject_fault("before_evidence")
    try:
        evidence_stored = artifacts.write_text_or_verify(
            run_id,
            evidence_rel,
            evidence_text,
            max_bytes=max(4096, len(evidence_text.encode("utf-8"))),
        )
    except ProtectedArtifactError as exc:
        raise CodexReviewPromptEvidenceError(str(exc)) from exc
    _maybe_inject_fault("after_evidence")

    if evidence_stored.sha256 != hashlib.sha256(evidence_text.encode("utf-8")).hexdigest():
        raise CodexReviewPromptEvidenceError("evidence digest mismatch after publish")
    if prompt_stored.sha256 != hashlib.sha256(prompt_bytes).hexdigest():
        raise CodexReviewPromptEvidenceError("prompt digest mismatch after publish")

    from ai_dev_loop.scheduler.infrastructure.paths import resolve_run_relative_path

    root = artifacts.run_root(run_id)
    prompt_path = resolve_run_relative_path(root, prompt_rel)
    evidence_path = resolve_run_relative_path(root, evidence_rel)
    verify_file_digest(
        prompt_path,
        expected_sha256=prompt_stored.sha256,
        expected_size=prompt_stored.size_bytes,
    )
    verify_file_digest(
        evidence_path,
        expected_sha256=evidence_stored.sha256,
        expected_size=len(evidence_text.encode("utf-8")),
    )
    _maybe_inject_fault("before_launch")
    return prompt_stored, evidence_stored


def reviewer_session_ref(session_id: str | None) -> str | None:
    if not session_id or not session_id.strip():
        return None
    material = f"ai_dev_loop:reviewer-session:v1:{session_id.strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
