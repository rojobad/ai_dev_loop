"""CLI rendering and command entry points for the central scheduler."""

from __future__ import annotations

import json

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerRunSummary,
    SchedulerStatusResult,
    StartResult,
    SubmitResult,
    TickReceipt,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.status import scheduler_list, scheduler_status
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import run_scheduler_tick


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
    lines = [
        f"Run: {summary.run_id}",
        f"State: {summary.state_kind}",
        f"Project: {summary.project_name}",
        f"Repository: {summary.repository_root}",
        f"Controller: {summary.controller_session_id_prefix}",
        f"Reviewer: {summary.reviewer_session_id_prefix}",
        f"Submitted: {summary.submitted_at}",
        f"Updated: {summary.updated_at}",
    ]
    if summary.cursor_wait_until:
        lines.append(f"Cursor wait until: {summary.cursor_wait_until}")
    if summary.block_reason_kind:
        lines.append(f"Block reason: {summary.block_reason_kind}")
    lines.append(f"Next action: {summary.safe_next_action.command}")
    return lines


def render_start_output(result: StartResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "run_id": result.run_id,
            "state_kind": result.state_kind,
            "changed": result.changed,
            "idempotent_replay": result.idempotent_replay,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    changed = " (no change)" if not result.changed else ""
    replay = " (idempotent replay)" if result.idempotent_replay else ""
    lines = [
        f"Scheduler start for run {result.run_id}{changed}{replay}",
        f"State: {result.state_kind}",
        f"Next action: {result.safe_next_action.command}",
    ]
    return "\n".join(lines) + "\n"


def render_tick_output(result: TickReceipt, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "tick_owner_id_prefix": result.tick_owner_id[:8],
            "lease_generation": result.lease_generation,
            "lease_acquired": result.lease_acquired,
            "visited_runs": result.visited_runs,
            "run_receipts": [item.model_dump(mode="json") for item in result.run_receipts],
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        "Scheduler tick completed",
        f"Lease acquired: {result.lease_acquired}",
        f"Visited runs: {result.visited_runs}",
    ]
    for receipt in result.run_receipts:
        detail = f" ({receipt.detail})" if receipt.detail else ""
        lines.append(f"- {receipt.run_id}: {receipt.action}{detail}")
    lines.append(f"Next action: {result.safe_next_action.command}")
    return "\n".join(lines) + "\n"


def render_status_output(result: SchedulerStatusResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "summary": result.summary.model_dump(mode="json"),
            "idempotency_key_prefix": result.idempotency_key_prefix,
            "worktree_key_prefix": result.worktree_key_prefix,
            "capacity_holder_run_id": result.capacity_holder_run_id,
            "last_event_kind": result.last_event_kind,
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = list(_render_summary(result.summary, output="text"))
    lines.append(f"Idempotency key prefix: {result.idempotency_key_prefix}")
    lines.append(f"Worktree key prefix: {result.worktree_key_prefix}")
    if result.last_event_kind:
        lines.append(f"Last event: {result.last_event_kind}")
    if result.capacity_holder_run_id:
        lines.append(f"Capacity holder run: {result.capacity_holder_run_id}")
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
    "render_start_output",
    "render_status_output",
    "render_submit_output",
    "render_tick_output",
    "run_scheduler_tick",
    "scheduler_list",
    "scheduler_status",
    "start_run",
    "submit_run",
]
