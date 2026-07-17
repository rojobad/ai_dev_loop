"""Codex turns for GitHub PR feedback adjudication and publication text."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.github_pr_review_result import GithubPrReviewResult
from ai_dev_loop.paths import schema_path
from ai_dev_loop.process import ActiveProcessRegistration, run_process_streaming
from ai_dev_loop.runners.codex import build_codex_review_args, redact_codex_args
from ai_dev_loop.runners.publish import PublicationText
from ai_dev_loop.state import RunState, atomic_write_json, atomic_write_text, sha256_text


@dataclass(frozen=True)
class GithubAdjudicationArtifacts:
    events_path: str
    stderr_path: str
    result_path: str
    report_path: str
    metadata_path: str
    snapshot_path: str
    fix_prompt_path: str | None


def build_github_feedback_prompt(
    state: RunState,
    run_directory: Path,
    *,
    external_review_skill: str,
    eligible_thread_payload: list[dict[str, Any]],
    bound_head_sha: str,
    pr_number: int,
) -> str:
    plan_snapshot = run_directory / state.plan.snapshot_path
    prompt_snapshot = run_directory / state.prompt.snapshot_path
    cursor_prompt = prompt_snapshot.read_text(encoding="utf-8")
    threads_json = json.dumps(eligible_thread_payload, indent=2, sort_keys=True)
    return (
        "This is an automated external-feedback adjudication turn for the existing "
        "approved plan and bound pull request.\n\n"
        f"Invoke the configured external review skill exactly as: ${external_review_skill}\n\n"
        "Evaluate every eligible GitHub review thread below. Do not invent threads.\n"
        "Decide each thread as actionable, not_applicable, or uncertain.\n"
        "If ANY thread is not_applicable or uncertain:\n"
        "- set all_actionable=false\n"
        "- set cursor_fix_prompt=null\n"
        "- provide an exact inline_reply for every non-actionable thread that begins "
        "with @rojobad and explains why it does not apply or what is missing\n"
        "If EVERY thread is actionable:\n"
        "- set all_actionable=true\n"
        "- set every inline_reply to null\n"
        "- return a complete English cursor_fix_prompt covering all threads\n\n"
        f"## PR binding\n- PR number: {pr_number}\n- Bound head SHA: {bound_head_sha}\n\n"
        "## Plan references\n"
        f"- Repository plan path: {state.plan.repository_path}\n"
        f"- Plan snapshot path: {plan_snapshot}\n\n"
        "## Original Cursor prompt\n\n"
        f"{cursor_prompt}\n\n"
        "## Eligible GitHub review threads (sensitive bodies included for adjudication)\n\n"
        f"{threads_json}\n\n"
        "Your final response must conform to github-pr-review-result-v1.json.\n"
        "eligible_thread_ids must exactly match the thread IDs provided.\n"
    )


def build_publication_text_prompt(
    state: RunState,
    *,
    staged_name_only: str,
    staged_stat: str,
    residual_risk_note: str | None,
) -> str:
    note = residual_risk_note or "none"
    return (
        "This is an automated publication-text turn for accepted staged changes.\n"
        "Author a concise git commit subject/body and a PR title/body for the bound branch.\n"
        "Do not include secrets, tokens, or full patch contents.\n"
        "If residual risk is recorded, mention it briefly in the PR body.\n\n"
        f"## Plan path\n{state.plan.repository_path}\n\n"
        f"## Staged name-only\n{staged_name_only}\n\n"
        f"## Staged stat\n{staged_stat}\n\n"
        f"## Residual risk note\n{note}\n\n"
        "Your final response must conform to github-publication-text-v1.json.\n"
    )


def _load_github_result(path: Path) -> GithubPrReviewResult:
    if not path.is_file():
        raise ValidationError(f"GitHub PR review result missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"GitHub PR review result is not valid JSON: {exc}") from exc
    try:
        return GithubPrReviewResult.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(f"GitHub PR review result validation failed: {exc}") from exc


def _load_publication_text(path: Path) -> PublicationText:
    if not path.is_file():
        raise ValidationError(f"publication text result missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"publication text is not valid JSON: {exc}") from exc
    try:
        return PublicationText(
            commit_subject=str(payload["commit_subject"]).strip(),
            commit_body=str(payload.get("commit_body") or ""),
            pr_title=str(payload["pr_title"]).strip(),
            pr_body=str(payload.get("pr_body") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"publication text validation failed: {exc}") from exc


def run_codex_github_review(
    state: RunState,
    run_directory: Path,
    *,
    cycle_number: int,
    external_review_skill: str,
    eligible_thread_payload: list[dict[str, Any]],
    bound_head_sha: str,
    pr_number: int,
) -> tuple[GithubPrReviewResult, GithubAdjudicationArtifacts]:
    if not state.codex.session_id:
        raise ValidationError("Codex session_id is required for GitHub adjudication")
    schema_file = schema_path("github-pr-review-result-v1.json")
    if not schema_file.is_file():
        raise AiDevLoopError(f"schema missing: {schema_file}")

    label = f"{cycle_number:02d}"
    events_rel = f"github/cycles/{label}/codex.events.jsonl"
    stderr_rel = f"github/cycles/{label}/codex.stderr.txt"
    result_rel = f"github/cycles/{label}/result.json"
    report_rel = f"github/cycles/{label}/report.md"
    metadata_rel = f"github/cycles/{label}/codex.metadata.json"
    snapshot_rel = f"github/cycles/{label}/threads.snapshot.json"

    result_path = run_directory / result_rel
    prompt = build_github_feedback_prompt(
        state,
        run_directory,
        external_review_skill=external_review_skill,
        eligible_thread_payload=eligible_thread_payload,
        bound_head_sha=bound_head_sha,
        pr_number=pr_number,
    )
    # Persist a redacted snapshot (hashes/ids only) plus a sensitive full snapshot.
    safe_snapshot = [
        {
            "thread_id": item["thread_id"],
            "author_login": item.get("author_login"),
            "path": item.get("path"),
            "commit_sha": item.get("commit_sha"),
            "body_sha256": sha256_text(str(item.get("body") or "")),
        }
        for item in eligible_thread_payload
    ]
    atomic_write_json(run_directory / snapshot_rel, {"threads": safe_snapshot}, sensitive=True)
    sensitive_snapshot = run_directory / f"github/cycles/{label}/threads.full.json"
    atomic_write_json(sensitive_snapshot, {"threads": eligible_thread_payload}, sensitive=True)

    args = build_codex_review_args(
        state.codex,
        repo_root=state.repository.root,
        session_id=state.codex.session_id,
        schema_file=schema_file,
        result_file=result_path,
    )
    timeout_seconds = state.workflow.codex_timeout_minutes * 60
    process = run_process_streaming(
        args,
        cwd=state.repository.root,
        timeout=timeout_seconds,
        stdin_text=prompt,
        stdout_path=run_directory / events_rel,
        stderr_path=run_directory / stderr_rel,
        sensitive=True,
        active_process=ActiveProcessRegistration(
            run_directory=run_directory,
            run_id=state.run_id,
            component="codex",
            iteration=cycle_number,
            argv_redacted=redact_codex_args(args),
        ),
    )
    atomic_write_json(
        run_directory / metadata_rel,
        {
            "args": redact_codex_args(args),
            "exit_code": process.returncode,
            "timed_out": process.timed_out,
            "session_id_prefix": state.codex.session_id[:8],
        },
        sensitive=True,
    )
    if process.timed_out:
        raise AiDevLoopError(
            f"Codex GitHub adjudication timed out; inspect {events_rel} and {stderr_rel}"
        )
    if process.returncode != 0:
        raise AiDevLoopError(
            f"Codex GitHub adjudication failed with exit code {process.returncode}; "
            f"inspect {events_rel} and {stderr_rel}"
        )
    review = _load_github_result(result_path)
    atomic_write_text(
        run_directory / report_rel,
        review.review_markdown,
        sensitive=True,
    )
    fix_prompt_path: str | None = None
    if review.all_actionable and review.cursor_fix_prompt:
        fix_prompt_path = f"prompts/fixes/github-{label}.txt"
        atomic_write_text(
            run_directory / fix_prompt_path,
            review.cursor_fix_prompt,
            sensitive=True,
        )
    artifacts = GithubAdjudicationArtifacts(
        events_path=events_rel,
        stderr_path=stderr_rel,
        result_path=result_rel,
        report_path=report_rel,
        metadata_path=metadata_rel,
        snapshot_path=snapshot_rel,
        fix_prompt_path=fix_prompt_path,
    )
    return review, artifacts


def run_codex_publication_text(
    state: RunState,
    run_directory: Path,
    *,
    cycle_number: int,
    staged_name_only: str,
    staged_stat: str,
    residual_risk_note: str | None = None,
) -> PublicationText:
    schema_file = schema_path("github-publication-text-v1.json")
    if not schema_file.is_file():
        raise AiDevLoopError(f"schema missing: {schema_file}")
    label = f"{cycle_number:02d}"
    result_rel = f"github/cycles/{label}/publication-text.json"
    result_path = run_directory / result_rel
    events_rel = f"github/cycles/{label}/publication.events.jsonl"
    stderr_rel = f"github/cycles/{label}/publication.stderr.txt"
    prompt = build_publication_text_prompt(
        state,
        staged_name_only=staged_name_only,
        staged_stat=staged_stat,
        residual_risk_note=residual_risk_note,
    )
    args = build_codex_review_args(
        state.codex,
        repo_root=state.repository.root,
        session_id=state.codex.session_id,
        schema_file=schema_file,
        result_file=result_path,
    )
    process = run_process_streaming(
        args,
        cwd=state.repository.root,
        timeout=state.workflow.codex_timeout_minutes * 60,
        stdin_text=prompt,
        stdout_path=run_directory / events_rel,
        stderr_path=run_directory / stderr_rel,
        sensitive=True,
        active_process=ActiveProcessRegistration(
            run_directory=run_directory,
            run_id=state.run_id,
            component="codex",
            iteration=cycle_number,
            argv_redacted=redact_codex_args(args),
        ),
    )
    if process.timed_out or process.returncode != 0:
        raise AiDevLoopError(
            f"Codex publication-text turn failed; inspect {events_rel} and {stderr_rel}"
        )
    return _load_publication_text(result_path)
