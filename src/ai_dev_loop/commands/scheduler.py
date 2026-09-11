"""CLI rendering and command entry points for the central scheduler."""

from __future__ import annotations

import json

from ai_dev_loop.scheduler.application.abort import scheduler_abort_run
from ai_dev_loop.scheduler.application.contracts import (
    AbortResult,
    HistoryResult,
    SchedulerRunSummary,
    SchedulerStatusResult,
    StartResult,
    SubmitResult,
    TickReceipt,
)
from ai_dev_loop.scheduler.application.cutover_cleanup import CutoverCleanupResult
from ai_dev_loop.scheduler.application.history import scheduler_history
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.status import scheduler_list, scheduler_status
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import run_scheduler_tick
from ai_dev_loop.scheduler.application.timer_ops import (
    TimerDisableResult,
    TimerInstallResult,
    TimerStatusResult,
)


def render_submit_output(result: SubmitResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "status": result.state_kind,
            "run_id": result.run_id,
            "project": result.project_name,
            "state_kind": result.state_kind,
            "reused_existing": result.reused_existing,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    if result.reused_existing:
        header = f"Scheduler run {result.run_id} (reused existing run)"
    else:
        header = f"Submitted scheduler run {result.run_id}"
    lines = [
        header,
        f"Project: {result.project_name}",
        f"State: {result.state_kind}",
    ]
    if result.state_kind == "queued":
        lines.extend(
            [
                "Submit binds the repository target only; worktree admission runs once at the first tick (Phase 17.2).",
                "Reviewer B is created at the first review boundary (Phase 17.5); submit only freezes model and reasoning.",
            ]
        )
    if result.safe_next_action.command:
        lines.append(f"Next action: {result.safe_next_action.command}")
    else:
        lines.append(f"Next action: {result.safe_next_action.kind.value}")
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


def render_scheduler_abort_output(result: AbortResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "run_id": result.run_id,
            "state_kind": result.state_kind,
            "abort_persisted": result.abort_persisted,
            "idempotent_replay": result.idempotent_replay,
            "process_action": result.process_action.value,
            "termination_pending": result.termination_pending,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    replay = " (idempotent replay)" if result.idempotent_replay else ""
    lines = [
        f"Scheduler abort for run {result.run_id}{replay}",
        f"State: {result.state_kind}",
        f"Abort persisted: {result.abort_persisted}",
        f"Process action: {result.process_action.value}",
        f"Termination pending: {result.termination_pending}",
        f"Next action: {result.safe_next_action.command}",
    ]
    return "\n".join(lines) + "\n"


def render_scheduler_history_output(result: HistoryResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "run_id": result.run_id,
            "order": result.order,
            "limit": result.limit,
            "truncated": result.truncated,
            "entries": [entry.model_dump(mode="json") for entry in result.entries],
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Scheduler history for run {result.run_id}",
        f"Order: {result.order}",
        f"Limit: {result.limit}",
        f"Truncated: {result.truncated}",
    ]
    for entry in result.entries:
        lines.append(
            f"- #{entry.sequence} {entry.created_at} {entry.event_kind}: {entry.safe_detail}"
        )
    return "\n".join(lines) + "\n"


def render_cutover_cleanup_output(result: CutoverCleanupResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "resolved_state_root": result.resolved_state_root,
            "deleted_paths": list(result.deleted_paths),
            "already_absent": list(result.already_absent),
            "dry_run": result.dry_run,
            "irrecoverable": not result.dry_run and bool(result.deleted_paths),
        }
        return json.dumps(payload, indent=2) + "\n"
    mode = "dry-run" if result.dry_run else "deleted"
    lines = [
        f"Legacy state cutover cleanup ({mode})",
        f"State root: {result.resolved_state_root}",
    ]
    if result.deleted_paths:
        lines.append("Targets removed:")
        lines.extend(f"- {path}" for path in result.deleted_paths)
    if result.already_absent:
        lines.append("Already absent:")
        lines.extend(f"- {path}" for path in result.already_absent)
    if not result.dry_run and result.deleted_paths:
        lines.append("Deleted legacy state is not recoverable.")
    return "\n".join(lines) + "\n"


def render_timer_install_output(result: TimerInstallResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "service_unit_path": result.service_unit_path,
            "timer_unit_path": result.timer_unit_path,
            "installed": result.installed,
            "enabled": result.enabled,
            "reloaded": result.reloaded,
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        "Scheduler timer install completed",
        f"Service unit: {result.service_unit_path}",
        f"Timer unit: {result.timer_unit_path}",
        f"Installed or refreshed: {result.installed}",
        f"Enabled now: {result.enabled}",
        "Use `ai_dev_loop scheduler timer status` before enabling in production.",
    ]
    return "\n".join(lines) + "\n"


def render_timer_status_output(result: TimerStatusResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "service_unit_path": result.service_unit_path,
            "timer_unit_path": result.timer_unit_path,
            "service_installed": result.service_installed,
            "timer_installed": result.timer_installed,
            "service_content_matches": result.service_content_matches,
            "timer_content_matches": result.timer_content_matches,
            "timer_enabled": result.timer_enabled,
            "timer_active": result.timer_active,
            "service_active": result.service_active,
            "ownership_ok": result.ownership_ok,
            "detail": result.detail,
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        "Scheduler timer status",
        f"Service unit: {result.service_unit_path}",
        f"Timer unit: {result.timer_unit_path}",
        f"Installed: service={result.service_installed}, timer={result.timer_installed}",
        f"Content matches package: service={result.service_content_matches}, timer={result.timer_content_matches}",
        f"Ownership ok: {result.ownership_ok}",
        f"Timer enabled: {result.timer_enabled}",
        f"Timer active: {result.timer_active}",
        f"Service active: {result.service_active}",
    ]
    if result.detail:
        lines.append(f"Detail: {result.detail}")
    return "\n".join(lines) + "\n"


def render_timer_disable_output(result: TimerDisableResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "timer_unit_path": result.timer_unit_path,
            "disabled": result.disabled,
            "stopped": result.stopped,
            "detail": result.detail,
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        "Scheduler timer disabled",
        f"Timer unit: {result.timer_unit_path}",
        f"Disabled: {result.disabled}",
        f"Stopped: {result.stopped}",
    ]
    if result.detail:
        lines.append(f"Detail: {result.detail}")
    return "\n".join(lines) + "\n"


def render_timer_validate_output(errors: list[str], *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "ok": not errors,
            "errors": errors,
        }
        return json.dumps(payload, indent=2) + "\n"
    if not errors:
        return "Scheduler timer assets validated successfully.\n"
    lines = ["Scheduler timer asset validation failed:"]
    lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines) + "\n"


__all__ = [
    "SubmitOptions",
    "render_cutover_cleanup_output",
    "render_scheduler_abort_output",
    "render_scheduler_history_output",
    "render_list_output",
    "render_start_output",
    "render_status_output",
    "render_submit_output",
    "render_tick_output",
    "render_timer_disable_output",
    "render_timer_install_output",
    "render_timer_status_output",
    "render_timer_validate_output",
    "run_scheduler_tick",
    "scheduler_abort_run",
    "scheduler_history",
    "scheduler_list",
    "scheduler_status",
    "start_run",
    "submit_run",
]
