"""Pure Codex review inspection projections for the Integration API."""

# mypy: disable-error-code=call-arg

from __future__ import annotations

import sqlite3
from pathlib import Path

from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.models import (
    IntegrationContentAvailability,
    IntegrationProcessOutputAvailability,
    IntegrationReviewContentAvailability,
    IntegrationReviewDetailData,
    IntegrationReviewListItem,
    IntegrationReviewResponseBody,
)
from ai_dev_loop.integration_api.process_output_auth import ProcessOutputIntegrityError
from ai_dev_loop.integration_api.process_output_resolution import (
    authenticate_for_output,
    resolve_process_stream,
    stream_availability_reason,
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


def _review_process_output(
    run_root: Path,
    *,
    attempt_row: sqlite3.Row,
    effect_kind: str,
) -> IntegrationProcessOutputAvailability:
    from ai_dev_loop.integration_api.models import IntegrationProcessStreamAvailability

    try:
        auth = authenticate_for_output(run_root, attempt_row, effect_kind)
        stdout_resolved = resolve_process_stream(
            run_root,
            attempt_row=attempt_row,
            effect_kind=effect_kind,
            stream="stdout",
            auth=auth,
        )
        stderr_resolved = resolve_process_stream(
            run_root,
            attempt_row=attempt_row,
            effect_kind=effect_kind,
            stream="stderr",
            auth=auth,
        )
        stdout_reason = stderr_reason = None
        try:
            stdout_reason = stream_availability_reason(
                run_root,
                resolved=stdout_resolved,
                auth=auth,
                attempt_status=str(attempt_row["status"]),
            )
            stderr_reason = stream_availability_reason(
                run_root,
                resolved=stderr_resolved,
                auth=auth,
                attempt_status=str(attempt_row["status"]),
            )
        except ProcessOutputIntegrityError:
            return IntegrationProcessOutputAvailability(
                stdout=IntegrationProcessStreamAvailability(
                    stream="stdout",
                    available=False,
                    reason="data_integrity",
                ),
                stderr=IntegrationProcessStreamAvailability(
                    stream="stderr",
                    available=False,
                    reason="data_integrity",
                ),
            )
        stdout_avail = (
            IntegrationProcessStreamAvailability(stream="stdout", available=True, reason=None)
            if stdout_reason is None
            else IntegrationProcessStreamAvailability(
                stream="stdout", available=False, reason=stdout_reason
            )
        )
        stderr_avail = (
            IntegrationProcessStreamAvailability(stream="stderr", available=True, reason=None)
            if stderr_reason is None
            else IntegrationProcessStreamAvailability(
                stream="stderr", available=False, reason=stderr_reason
            )
        )
        return IntegrationProcessOutputAvailability(stdout=stdout_avail, stderr=stderr_avail)
    except IntegrationApiError:
        return IntegrationProcessOutputAvailability(
            stdout=IntegrationProcessStreamAvailability(
                stream="stdout",
                available=False,
                reason="data_integrity",
            ),
            stderr=IntegrationProcessStreamAvailability(
                stream="stderr",
                available=False,
                reason="data_integrity",
            ),
        )


def build_review_list_item(
    *,
    run_id: str,
    attempt_row: sqlite3.Row,
    effect_kind: str,
    auth: AuthenticatedReviewEvidence,
    run_root: Path | None = None,
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
    from ai_dev_loop.integration_api.models import IntegrationProcessStreamAvailability

    if run_root is not None:
        process_output = _review_process_output(
            run_root,
            attempt_row=attempt_row,
            effect_kind=effect_kind,
        )
    else:
        process_output = IntegrationProcessOutputAvailability(
            stdout=IntegrationProcessStreamAvailability(
                stream="stdout",
                available=False,
                reason="unavailable",
            ),
            stderr=IntegrationProcessStreamAvailability(
                stream="stderr",
                available=False,
                reason="unavailable",
            ),
        )
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
        process_output=process_output,
    )


def build_review_detail_data(
    *,
    run_id: str,
    attempt_row: sqlite3.Row,
    effect_kind: str,
    auth: AuthenticatedReviewEvidence,
    run_root: Path | None = None,
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
    from ai_dev_loop.integration_api.models import IntegrationProcessStreamAvailability

    if run_root is not None:
        process_output = _review_process_output(
            run_root,
            attempt_row=attempt_row,
            effect_kind=effect_kind,
        )
    else:
        process_output = IntegrationProcessOutputAvailability(
            stdout=IntegrationProcessStreamAvailability(
                stream="stdout",
                available=False,
                reason="unavailable",
            ),
            stderr=IntegrationProcessStreamAvailability(
                stream="stderr",
                available=False,
                reason="unavailable",
            ),
        )
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
        process_output=process_output,
    )


def confined_run_root(artifact_root: Path, run_id: str) -> Path:
    return readonly_confined_run_artifact_root(artifact_root, run_id)
