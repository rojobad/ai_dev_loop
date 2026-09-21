"""Pure Codex review inspection projections for the Integration API."""

# mypy: disable-error-code=call-arg

from __future__ import annotations

import sqlite3
from pathlib import Path

from ai_dev_loop.integration_api.models import (
    IntegrationContentAvailability,
    IntegrationReviewContentAvailability,
    IntegrationReviewDetailData,
    IntegrationReviewListItem,
    IntegrationReviewResponseBody,
)
from ai_dev_loop.integration_api.review_evidence_auth import (
    AuthenticatedReviewEvidence,
    reviewer_ref_from_auth,
)
from ai_dev_loop.scheduler.application.timeline import _timeline_status
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    RESUME_CODEX_REVIEW_EFFECT_KIND,
)
from ai_dev_loop.scheduler.infrastructure.paths import readonly_confined_run_artifact_root


def _availability(available: bool, *, reason: str | None = None) -> IntegrationContentAvailability:
    if available:
        return IntegrationContentAvailability(available=True, reason=None)
    return IntegrationContentAvailability(available=False, reason=reason or "unavailable")


def _review_mode(effect_kind: str) -> str:
    if effect_kind == BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND:
        return "bootstrap"
    if effect_kind == RESUME_CODEX_REVIEW_EFFECT_KIND:
        return "resume"
    return "unknown"


def _content_availability(
    auth: AuthenticatedReviewEvidence,
) -> IntegrationReviewContentAvailability:
    if auth.prompt_state == "available":
        prompt_avail = _availability(True)
    elif auth.prompt_state == "not_yet_produced":
        prompt_avail = _availability(False, reason="not_yet_produced")
    elif auth.prompt_state == "incomplete_capture":
        prompt_avail = _availability(False, reason="incomplete_capture")
    elif auth.prompt_state == "data_integrity":
        prompt_avail = _availability(False, reason="data_integrity")
    else:
        prompt_avail = _availability(False, reason="not_recorded")

    if auth.result_state == "data_integrity":
        response_avail = _availability(False, reason="data_integrity")
    elif auth.result_state == "pending":
        response_avail = _availability(False, reason="not_yet_produced")
    elif auth.result_state in {"valid", "invalid"} and auth.outcome is not None:
        response_avail = _availability(True)
    else:
        response_avail = _availability(False, reason="not_produced")

    if auth.result_state == "valid" and auth.validated is not None:
        markdown_avail = _availability(True)
        if auth.validated.cursor_fix_prompt:
            fix_avail = _availability(True)
        else:
            fix_avail = _availability(False, reason="not_applicable")
    elif auth.result_state == "invalid":
        markdown_avail = _availability(False, reason="invalid_result")
        fix_avail = _availability(False, reason="invalid_result")
    elif auth.result_state == "data_integrity":
        markdown_avail = _availability(False, reason="data_integrity")
        fix_avail = _availability(False, reason="data_integrity")
    elif auth.result_state == "pending":
        markdown_avail = _availability(False, reason="not_yet_produced")
        fix_avail = _availability(False, reason="not_yet_produced")
    else:
        markdown_avail = _availability(False, reason="not_produced")
        fix_avail = _availability(False, reason="not_produced")

    return IntegrationReviewContentAvailability(
        prompt=prompt_avail,
        response=response_avail,
        review_markdown=markdown_avail,
        cursor_fix_prompt=fix_avail,
    )


def build_review_list_item(
    *,
    run_id: str,
    attempt_row: sqlite3.Row,
    effect_kind: str,
    auth: AuthenticatedReviewEvidence,
) -> IntegrationReviewListItem:
    launch = (
        str(attempt_row["launch_requested_at"])
        if attempt_row["launch_requested_at"] is not None
        else None
    )
    completed = (
        str(attempt_row["completed_at"]) if attempt_row["completed_at"] is not None else None
    )
    invocation = auth.invocation
    review_model = str(invocation.get("review_model", ""))
    reasoning = str(invocation.get("review_reasoning_effort", ""))
    content = _content_availability(auth)
    findings_count: int | None = None
    highest: str | None = None
    tests_status: str | None = None
    if auth.result_state == "valid" and auth.validated is not None:
        findings_count = auth.validated.findings_count
        highest = auth.validated.highest_severity
        tests_status = auth.validated.tests_status
    return IntegrationReviewListItem(
        attempt_id=str(attempt_row["attempt_id"]),
        iteration=int(attempt_row["iteration"]),
        phase_attempt=int(attempt_row["phase_attempt"]),
        status=_timeline_status(str(attempt_row["status"])),
        review_mode=_review_mode(effect_kind),
        review_model=review_model,
        reasoning_effort=reasoning,
        created_at=str(attempt_row["created_at"]),
        launch_requested_at=launch,
        completed_at=completed,
        findings_count=findings_count,
        highest_severity=highest,
        tests_status=tests_status,
        reviewer_session_ref=reviewer_ref_from_auth(auth),
        content=content,
    )


def build_review_detail_data(
    *,
    run_id: str,
    attempt_row: sqlite3.Row,
    effect_kind: str,
    auth: AuthenticatedReviewEvidence,
) -> IntegrationReviewDetailData:
    launch = (
        str(attempt_row["launch_requested_at"])
        if attempt_row["launch_requested_at"] is not None
        else None
    )
    completed = (
        str(attempt_row["completed_at"]) if attempt_row["completed_at"] is not None else None
    )
    invocation = auth.invocation
    review_model = str(invocation.get("review_model", ""))
    reasoning = str(invocation.get("review_reasoning_effort", ""))
    content = _content_availability(auth)
    findings_count: int | None = None
    highest: str | None = None
    tests_status: str | None = None
    response_body: IntegrationReviewResponseBody | None = None
    if auth.result_state == "valid" and auth.validated is not None:
        findings_count = auth.validated.findings_count
        highest = auth.validated.highest_severity
        tests_status = auth.validated.tests_status
        response_body = IntegrationReviewResponseBody(
            has_actionable_findings=auth.validated.has_actionable_findings,
            findings_count=auth.validated.findings_count,
            highest_severity=auth.validated.highest_severity,
            review_markdown=auth.validated.review_markdown,
            cursor_fix_prompt=auth.validated.cursor_fix_prompt,
            tests_status=auth.validated.tests_status,
            summary=auth.validated.summary,
        )
    return IntegrationReviewDetailData(
        run_id=run_id,
        attempt_id=str(attempt_row["attempt_id"]),
        iteration=int(attempt_row["iteration"]),
        phase_attempt=int(attempt_row["phase_attempt"]),
        status=_timeline_status(str(attempt_row["status"])),
        review_mode=_review_mode(effect_kind),
        review_model=review_model,
        reasoning_effort=reasoning,
        created_at=str(attempt_row["created_at"]),
        launch_requested_at=launch,
        completed_at=completed,
        findings_count=findings_count,
        highest_severity=highest,
        tests_status=tests_status,
        reviewer_session_ref=reviewer_ref_from_auth(auth),
        result_state=auth.result_state,
        response=response_body,
        content=content,
    )


def confined_run_root(artifact_root: Path, run_id: str) -> Path:
    return readonly_confined_run_artifact_root(artifact_root, run_id)
