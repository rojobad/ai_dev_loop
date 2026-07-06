"""Codex review execution, prompt construction, and artifact persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.abort_control import is_abort_requested
from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.iterations import find_iteration, upsert_iteration
from ai_dev_loop.paths import schema_path, set_sensitive_file_mode
from ai_dev_loop.process import (
    ActiveProcessRegistration,
    StreamingProcessResult,
    run_process_streaming,
)
from ai_dev_loop.review_result import CodexReviewResult, completion_status_for_review
from ai_dev_loop.state import CodexState, RunState, atomic_write_json, atomic_write_text

PHASE_5_NO_FINDINGS_MESSAGE = (
    "Codex review found no actionable findings. Changes remain staged in the target repository."
)
PHASE_5_RESIDUAL_RISK_MESSAGE = (
    "Codex review found no actionable findings, but tests failed, were blocked, or reported "
    "residual risk. Changes remain staged in the target repository."
)
PHASE_5_FINDINGS_CONTINUE_MESSAGE = (
    "Codex review found actionable findings. Continuing with the stored Cursor correction prompt."
)
PHASE_5_MAX_ITERATIONS_MESSAGE = (
    "Maximum review iterations reached. Latest fix prompt and review artifacts are stored. "
    "Changes remain staged in the target repository."
)

# Backward-compatible aliases used by existing tests during transition.
PHASE_4_NO_FINDINGS_MESSAGE = PHASE_5_NO_FINDINGS_MESSAGE
PHASE_4_RESIDUAL_RISK_MESSAGE = PHASE_5_RESIDUAL_RISK_MESSAGE
PHASE_4_FINDINGS_MESSAGE = (
    "Codex review found actionable findings. A Cursor correction prompt is stored."
)


@dataclass(frozen=True)
class CodexReviewArtifacts:
    events_path: str
    stderr_path: str
    result_path: str
    report_path: str
    metadata_path: str
    fix_prompt_path: str | None


@dataclass(frozen=True)
class CodexReviewExecution:
    process: StreamingProcessResult
    result: CodexReviewResult
    artifacts: CodexReviewArtifacts
    metadata_args: list[str]
    completion_status: str


def _codex_failure_message(
    *,
    exit_code: int,
    events_path: str,
    stderr_path: str,
) -> str:
    return (
        f"Codex review failed with exit code {exit_code}; inspect {events_path} and {stderr_path}"
    )


def _codex_timeout_message(*, events_path: str, stderr_path: str) -> str:
    return f"Codex review timed out; inspect {events_path} and {stderr_path}"


def build_codex_review_args(
    codex: CodexState,
    *,
    repo_root: str,
    session_id: str,
    schema_file: Path,
    result_file: Path,
) -> list[str]:
    return [
        codex.command,
        "exec",
        "--cd",
        repo_root,
        "--sandbox",
        codex.sandbox,
        "resume",
        "--model",
        codex.review_model,
        "--json",
        "--output-schema",
        str(schema_file),
        "--output-last-message",
        str(result_file),
        session_id,
        "-",
    ]


def redact_codex_args(args: list[str]) -> list[str]:
    redacted = list(args)
    if "-" in redacted:
        redacted[redacted.index("-")] = "<stdin-prompt>"
    return redacted


def build_review_wrapper_prompt(
    state: RunState,
    run_directory: Path,
    *,
    iteration: str,
    cursor_final_response: str | None,
) -> str:
    review_skill = state.codex.review_skill
    plan_snapshot = run_directory / state.plan.snapshot_path
    prompt_snapshot = run_directory / state.prompt.snapshot_path
    cursor_events = run_directory / f"cursor/iterations/{iteration}/events.jsonl"
    cursor_final = run_directory / f"cursor/iterations/{iteration}/final.txt"
    cursor_metadata = run_directory / f"cursor/iterations/{iteration}/metadata.json"
    staged_stat = run_directory / f"git/diffs/{iteration}.stat"
    staged_name_only = run_directory / f"git/diffs/{iteration}.name-only.txt"
    staged_patch = run_directory / f"git/diffs/{iteration}.patch"

    cursor_prompt = prompt_snapshot.read_text(encoding="utf-8")
    if cursor_final_response is not None:
        final_response_section = f"## Latest Cursor final response\n\n{cursor_final_response}\n"
    else:
        final_response_section = (
            "## Latest Cursor final response\n\n"
            "The orchestrator could not extract a final Cursor response from stream-json output. "
            "Review the staged changes and artifacts using the paths below.\n"
        )

    return (
        "This is an automated Codex review turn for the existing approved plan.\n\n"
        f"Invoke the configured review skill exactly as: ${review_skill}\n\n"
        "Review the current staged changes only. Do not review unrelated repository state.\n\n"
        "Follow the review skill's read-only discipline. Run tests only according to that skill.\n\n"
        "## Plan references\n"
        f"- Repository plan path: {state.plan.repository_path}\n"
        f"- Plan snapshot path: {plan_snapshot}\n\n"
        "## Original Cursor prompt\n\n"
        f"{cursor_prompt}\n\n"
        f"{final_response_section}\n"
        "## Artifact paths for auditability\n"
        f"- Plan snapshot: {plan_snapshot}\n"
        f"- Prompt snapshot: {prompt_snapshot}\n"
        f"- Cursor events: {cursor_events}\n"
        f"- Cursor final message: {cursor_final}\n"
        f"- Cursor metadata: {cursor_metadata}\n"
        f"- Staged diff stat: {staged_stat}\n"
        f"- Staged name-only diff: {staged_name_only}\n"
        f"- Staged patch: {staged_patch}\n\n"
        "Your final response must conform to codex-review-result-v1.json.\n"
        "When actionable findings exist, return a complete English cursor_fix_prompt.\n"
        "When there are no actionable findings, return cursor_fix_prompt: null.\n"
    )


def _read_cursor_final_response(run_directory: Path, iteration: str) -> str | None:
    final_path = run_directory / f"cursor/iterations/{iteration}/final.txt"
    if not final_path.is_file():
        return None
    text = final_path.read_text(encoding="utf-8").strip()
    return text or None


def _artifact_paths(iteration: str, *, fix_prompt_path: str | None) -> CodexReviewArtifacts:
    return CodexReviewArtifacts(
        events_path=f"codex/events/{iteration}.jsonl",
        stderr_path=f"codex/events/{iteration}.stderr.txt",
        result_path=f"codex/reviews/{iteration}.json",
        report_path=f"codex/reviews/{iteration}.md",
        metadata_path=f"codex/reviews/{iteration}.metadata.json",
        fix_prompt_path=fix_prompt_path,
    )


def _load_review_result(result_file: Path) -> CodexReviewResult:
    if not result_file.is_file():
        raise ValidationError(f"Codex review result file missing: {result_file}")
    try:
        payload = json.loads(result_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Codex review result is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("Codex review result must be a JSON object")
    try:
        return CodexReviewResult.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(f"Codex review result validation failed: {exc}") from exc


def _update_iteration_review(
    state: RunState,
    *,
    iteration: str,
    artifacts: CodexReviewArtifacts,
    exit_code: int,
    review: CodexReviewResult,
) -> None:
    number = int(iteration)
    existing = find_iteration(state, number)
    if existing is None:
        raise ValidationError(
            f"iteration metadata is missing before Codex review update: {iteration}"
        )
    entry = dict(existing)
    codex_section: dict[str, Any] = {
        "events_path": artifacts.events_path,
        "stderr_path": artifacts.stderr_path,
        "result_path": artifacts.result_path,
        "report_path": artifacts.report_path,
        "metadata_path": artifacts.metadata_path,
        "exit_code": exit_code,
    }
    if artifacts.fix_prompt_path is not None:
        codex_section["fix_prompt_path"] = artifacts.fix_prompt_path
    entry["codex"] = codex_section
    entry["review"] = {
        "has_actionable_findings": review.has_actionable_findings,
        "findings_count": review.findings_count,
        "highest_severity": review.highest_severity,
        "tests_status": review.tests_status,
        "summary": review.summary,
    }
    entry["completed_at"] = datetime.now(tz=UTC).isoformat()
    upsert_iteration(state, entry)


def run_codex_review(
    state: RunState,
    run_directory: Path,
    *,
    iteration: str = "01",
) -> CodexReviewExecution:
    repo_root = state.repository.root
    schema_file = schema_path("codex-review-result-v1.json")
    if not schema_file.is_file():
        raise AiDevLoopError(f"review schema missing: {schema_file}")

    events_rel = f"codex/events/{iteration}.jsonl"
    stderr_rel = f"codex/events/{iteration}.stderr.txt"
    result_rel = f"codex/reviews/{iteration}.json"
    report_rel = f"codex/reviews/{iteration}.md"
    metadata_rel = f"codex/reviews/{iteration}.metadata.json"

    events_path = run_directory / events_rel
    stderr_path = run_directory / stderr_rel
    result_path = run_directory / result_rel
    report_path = run_directory / report_rel
    metadata_path = run_directory / metadata_rel

    cursor_final_response = _read_cursor_final_response(run_directory, iteration)
    prompt = build_review_wrapper_prompt(
        state,
        run_directory,
        iteration=iteration,
        cursor_final_response=cursor_final_response,
    )
    args = build_codex_review_args(
        state.codex,
        repo_root=repo_root,
        session_id=state.codex.session_id,
        schema_file=schema_file,
        result_file=result_path,
    )
    timeout_seconds = state.workflow.codex_timeout_minutes * 60
    iteration_number = int(iteration)
    process = run_process_streaming(
        args,
        cwd=repo_root,
        timeout=timeout_seconds,
        stdin_text=prompt,
        stdout_path=events_path,
        stderr_path=stderr_path,
        sensitive=True,
        active_process=ActiveProcessRegistration(
            run_directory=run_directory,
            run_id=state.run_id,
            component="codex",
            iteration=iteration_number,
            argv_redacted=redact_codex_args(args),
        ),
    )

    metadata_payload = {
        "args": redact_codex_args(args),
        "exit_code": process.returncode,
        "elapsed_seconds": process.elapsed_seconds,
        "timed_out": process.timed_out,
        "session_id": state.codex.session_id,
        "review_skill": state.codex.review_skill,
    }
    atomic_write_json(metadata_path, metadata_payload, sensitive=True)
    set_sensitive_file_mode(metadata_path)

    if is_abort_requested(run_directory):
        raise AiDevLoopError(f"Codex review aborted; inspect {events_rel} and {stderr_rel}")

    if process.timed_out:
        raise AiDevLoopError(_codex_timeout_message(events_path=events_rel, stderr_path=stderr_rel))

    if process.returncode != 0:
        raise AiDevLoopError(
            _codex_failure_message(
                exit_code=process.returncode,
                events_path=events_rel,
                stderr_path=stderr_rel,
            )
        )

    review = _load_review_result(result_path)
    atomic_write_text(report_path, review.review_markdown + "\n", sensitive=True)
    set_sensitive_file_mode(report_path)

    fix_prompt_rel: str | None = None
    if review.has_actionable_findings:
        fix_prompt_rel = f"prompts/fixes/{iteration}.txt"
        atomic_write_text(
            run_directory / fix_prompt_rel,
            review.cursor_fix_prompt or "",
            sensitive=True,
        )

    artifacts = _artifact_paths(iteration, fix_prompt_path=fix_prompt_rel)
    _update_iteration_review(
        state,
        iteration=iteration,
        artifacts=artifacts,
        exit_code=process.returncode,
        review=review,
    )

    completion_status = completion_status_for_review(review)
    return CodexReviewExecution(
        process=process,
        result=review,
        artifacts=artifacts,
        metadata_args=redact_codex_args(args),
        completion_status=completion_status,
    )


def load_review_result_from_artifacts(run_directory: Path, iteration: str) -> CodexReviewResult:
    return _load_review_result(run_directory / f"codex/reviews/{iteration}.json")


def result_message_for_review(review: CodexReviewResult) -> str:
    if review.has_actionable_findings:
        return PHASE_4_FINDINGS_MESSAGE
    if review.tests_status in {"failed", "blocked_environment", "skipped_findings_present"}:
        return PHASE_5_RESIDUAL_RISK_MESSAGE
    return PHASE_5_NO_FINDINGS_MESSAGE


def result_message_for_loop_continue() -> str:
    return PHASE_5_FINDINGS_CONTINUE_MESSAGE


def result_message_for_max_iterations() -> str:
    return PHASE_5_MAX_ITERATIONS_MESSAGE
