"""CLI rendering and command entry points for the central scheduler."""

from __future__ import annotations

import json

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerRunSummary,
    SchedulerStatusResult,
    SubmitResult,
)
from ai_dev_loop.scheduler.application.status import scheduler_list, scheduler_status
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run


def render_submit_output(result: SubmitResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "status": "queued",
            "run_id": result.run_id,
            "project": result.project_name,
            "state_kind": result.state_kind,
            "reused_existing": result.reused_existing,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    reused = " (reused existing run)" if result.reused_existing else ""
    lines = [
        f"Submitted scheduler run {result.run_id}{reused}",
        f"Project: {result.project_name}",
        f"State: {result.state_kind}",
        "Submit binds the repository target only; worktree admission runs once at the first tick (Phase 17.2).",
        "Reviewer B is created at the first review boundary (Phase 17.5); submit only freezes model and reasoning.",
        f"Next action: {result.safe_next_action.command}",
    ]
    return "\n".join(lines) + "\n"


def _render_summary(summary: SchedulerRunSummary, *, output: str) -> dict[str, object] | list[str]:
    if output == "json":
        return summary.model_dump(mode="json")
    return [
        f"Run: {summary.run_id}",
        f"State: {summary.state_kind}",
        f"Project: {summary.project_name}",
        f"Repository: {summary.repository_root}",
        f"Controller: {summary.controller_session_id_prefix}",
        f"Reviewer: {summary.reviewer_session_id_prefix}",
        f"Submitted: {summary.submitted_at}",
        f"Updated: {summary.updated_at}",
        f"Next action: {summary.safe_next_action.command}",
    ]


def render_status_output(result: SchedulerStatusResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "summary": result.summary.model_dump(mode="json"),
            "idempotency_key_prefix": result.idempotency_key_prefix,
            "worktree_key_prefix": result.worktree_key_prefix,
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = list(_render_summary(result.summary, output="text"))
    lines.append(f"Idempotency key prefix: {result.idempotency_key_prefix}")
    lines.append(f"Worktree key prefix: {result.worktree_key_prefix}")
    return "\n".join(lines) + "\n"


def render_list_output(summaries: list[SchedulerRunSummary], *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "runs": [summary.model_dump(mode="json") for summary in summaries],
        }
        return json.dumps(payload, indent=2) + "\n"
    if not summaries:
        return "No scheduler runs found.\n"
    blocks: list[str] = []
    for summary in summaries:
        blocks.append("\n".join(_render_summary(summary, output="text")))
    return "\n\n".join(blocks) + "\n"


__all__ = [
    "SubmitOptions",
    "render_list_output",
    "render_status_output",
    "render_submit_output",
    "scheduler_list",
    "scheduler_status",
    "submit_run",
]
