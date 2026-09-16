"""CLI rendering and command entry points for the central scheduler."""

from __future__ import annotations

import json

from ai_dev_loop.scheduler.application.abort import scheduler_abort_run
from ai_dev_loop.scheduler.application.contracts import (
    AbortResult,
    HistoryResult,
    RecoveryAbortResult,
    RecoveryPrepareResult,
    RecoveryStartResult,
    RecoveryStatusResult,
    RolloverAbortResult,
    RolloverPrepareResult,
    RolloverStartResult,
    RolloverStatusResult,
    SchedulerRunSummary,
    SchedulerStatusResult,
    SequenceAbortResult,
    SequencePrepareResult,
    SequenceStartResult,
    SequenceStatusResult,
    StartResult,
    SubmitResult,
    TickReceipt,
    TimelineResult,
)
from ai_dev_loop.scheduler.application.cutover_cleanup import CutoverCleanupResult
from ai_dev_loop.scheduler.application.history import scheduler_history
from ai_dev_loop.scheduler.application.recovery_abort import abort_recovery
from ai_dev_loop.scheduler.application.recovery_prepare import prepare_recovery
from ai_dev_loop.scheduler.application.recovery_start import start_recovery
from ai_dev_loop.scheduler.application.recovery_status import recovery_status
from ai_dev_loop.scheduler.application.review_budget_extend import (
    ReviewBudgetExtendResult,
    scheduler_extend_review_budget,
)
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryResult, scheduler_review_retry
from ai_dev_loop.scheduler.application.rollover_abort import abort_rollover
from ai_dev_loop.scheduler.application.rollover_prepare import prepare_rollover
from ai_dev_loop.scheduler.application.rollover_start import start_rollover
from ai_dev_loop.scheduler.application.rollover_status import rollover_status
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    prepare_sequence,
)
from ai_dev_loop.scheduler.application.sequence_status import scheduler_sequence_status
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.status import scheduler_list, scheduler_status
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import run_scheduler_tick
from ai_dev_loop.scheduler.application.timeline import scheduler_timeline
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
        (
            f"Reviews completed: {summary.review_iterations_completed}/"
            f"{summary.max_review_iterations}"
            + (
                f" (submitted limit {summary.submitted_max_review_iterations})"
                if summary.submitted_max_review_iterations is not None
                else ""
            )
        ),
        *(
            [f"Controller: {summary.controller_session_id_prefix}"]
            if summary.controller_session_id_prefix
            else []
        ),
        f"Reviewer: {summary.reviewer_session_id_prefix}",
        f"Submitted: {summary.submitted_at}",
        f"Updated: {summary.updated_at}",
    ]
    if summary.sequence_id_prefix is not None:
        lines.append(
            "Sequence: "
            f"{summary.sequence_id_prefix} "
            f"phase {summary.sequence_ordinal}/{summary.sequence_total_phases}"
        )
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


def render_scheduler_timeline_output(result: TimelineResult, *, output: str) -> str:
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
        f"Scheduler timeline for run {result.run_id}",
        f"Order: {result.order}",
        f"Limit: {result.limit}",
        f"Truncated: {result.truncated}",
    ]
    for entry in result.entries:
        duration = (
            f"{entry.observed_duration_seconds}s"
            if entry.observed_duration_seconds is not None
            else "n/a"
        )
        lines.append(
            f"- iter {entry.iteration:02d} {entry.phase} "
            f"attempt {entry.phase_attempt} {entry.status} "
            f"launch={entry.launch_requested_at or 'n/a'} "
            f"completed={entry.completed_at or 'n/a'} duration={duration}"
        )
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


def render_sequence_prepare_output(result: SequencePrepareResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "sequence_id": result.sequence_id,
            "name": result.name,
            "state_kind": result.state_kind,
            "entry_count": result.entry_count,
            "reused_existing": result.reused_existing,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    if result.reused_existing:
        header = f"Scheduler sequence {result.sequence_id} (reused existing sequence)"
    else:
        header = f"Prepared scheduler sequence {result.sequence_id}"
    lines = [
        header,
        f"Name: {result.name}",
        f"State: {result.state_kind}",
        f"Entries: {result.entry_count}",
        "Sequence prepare freezes definitions only; it does not reserve the repository or invoke Git.",
        f"Next action: {result.safe_next_action.command}",
    ]
    return "\n".join(lines) + "\n"


def render_sequence_status_output(result: SequenceStatusResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "sequence_id": result.sequence_id,
            "name": result.name,
            "state_kind": result.state_kind,
            "project": result.project_name,
            "repository_root": result.repository_root,
            "entry_count": result.entry_count,
            "current_ordinal": result.current_ordinal,
            "current_run_id": result.current_run_id,
            "current_run_state_kind": result.current_run_state_kind,
            "current_phase_name": result.current_phase_name,
            "residual_risk": result.residual_risk,
            "residual_risk_ordinals": list(result.residual_risk_ordinals),
            "block_reason_kind": result.block_reason_kind,
            "abort_reason": result.abort_reason,
            "finalized_at": result.finalized_at,
            "completion_report_sha256_prefix": result.completion_report_sha256_prefix,
            "prepared_at": result.prepared_at,
            "updated_at": result.updated_at,
            "started_at": result.started_at,
            "idempotency_key_prefix": result.idempotency_key_prefix,
            "entries": [entry.model_dump(mode="json") for entry in result.entries],
            "aggregate_counts": (
                result.aggregate_counts.model_dump(mode="json")
                if result.aggregate_counts is not None
                else None
            ),
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Sequence: {result.sequence_id}",
        f"Name: {result.name}",
        f"State: {result.state_kind}",
        f"Project: {result.project_name}",
        f"Repository: {result.repository_root}",
        f"Entries: {result.entry_count}",
    ]
    if result.current_ordinal is not None:
        lines.append(f"Current phase: {result.current_ordinal:02d}/{result.entry_count}")
    if result.current_phase_name is not None:
        lines.append(f"Current phase name: {result.current_phase_name}")
    if result.current_run_id is not None:
        lines.append(f"Materialized run: {result.current_run_id}")
    if result.current_run_state_kind is not None:
        lines.append(f"Materialized run state: {result.current_run_state_kind}")
    if result.residual_risk is not None:
        lines.append(f"Residual risk: {result.residual_risk}")
    if result.residual_risk_ordinals:
        lines.append(
            f"Residual-risk phases: {', '.join(str(o) for o in result.residual_risk_ordinals)}"
        )
    if result.block_reason_kind:
        lines.append(f"Block reason: {result.block_reason_kind}")
    if result.abort_reason:
        lines.append(f"Abort reason: {result.abort_reason}")
    if result.finalized_at:
        lines.append(f"Finalized: {result.finalized_at}")
    if result.completion_report_sha256_prefix:
        lines.append(f"Completion report prefix: {result.completion_report_sha256_prefix}")
    if result.aggregate_counts is not None:
        counts = result.aggregate_counts
        lines.append(
            "Counts: "
            f"planned={counts.planned} materialized={counts.materialized} "
            f"accepted={counts.accepted} residual_risk={counts.residual_risk} "
            f"checkpointed={counts.checkpointed} cancelled={counts.cancelled} "
            f"remaining={counts.remaining}"
        )
    lines.extend(
        [
            f"Prepared: {result.prepared_at}",
            f"Updated: {result.updated_at}",
        ]
    )
    if result.started_at is not None:
        lines.append(f"Started: {result.started_at}")
    lines.append(f"Idempotency key prefix: {result.idempotency_key_prefix}")
    for entry in result.entries:
        commit_note = " (checkpoint commit message frozen)" if entry.commit_message_present else ""
        status_bits: list[str] = []
        if entry.materialized:
            status_bits.append("materialized")
        if entry.accepted_outcome:
            status_bits.append(entry.accepted_outcome)
        if entry.residual_risk:
            status_bits.append("residual_risk")
        if entry.checkpoint_commit_sha256_prefix:
            status_bits.append(f"checkpoint={entry.checkpoint_commit_sha256_prefix}")
        if entry.cancelled:
            status_bits.append("cancelled")
        status = f" [{', '.join(status_bits)}]" if status_bits else ""
        lines.append(
            f"- Phase {entry.ordinal:02d}: {entry.phase_name} "
            f"(planned run prefix {entry.planned_run_id_prefix}){commit_note}{status}"
        )
    lines.append(f"Next action: {result.safe_next_action.command}")
    return "\n".join(lines) + "\n"


def render_sequence_abort_output(result: SequenceAbortResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "sequence_id": result.sequence_id,
            "state_kind": result.state_kind,
            "abort_persisted": result.abort_persisted,
            "idempotent_replay": result.idempotent_replay,
            "run_abort_process_action": result.run_abort_process_action.value,
            "run_termination_pending": result.run_termination_pending,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    replay = " (idempotent replay)" if result.idempotent_replay else ""
    lines = [
        f"Scheduler sequence abort for {result.sequence_id}{replay}",
        f"State: {result.state_kind}",
        f"Abort persisted: {result.abort_persisted}",
        f"Run abort process action: {result.run_abort_process_action.value}",
        f"Run termination pending: {result.run_termination_pending}",
        f"Next action: {result.safe_next_action.command}",
    ]
    return "\n".join(lines) + "\n"


def render_review_budget_extend_output(result: ReviewBudgetExtendResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "run_id": result.run_id,
            "state_kind": result.state_kind,
            "changed": result.changed,
            "idempotent_replay": result.idempotent_replay,
            "previous_effective_total": result.previous_effective_total,
            "new_effective_total": result.new_effective_total,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Review budget extension run: {result.run_id}",
        f"State: {result.state_kind}",
        f"Changed: {result.changed}",
        f"Idempotent replay: {result.idempotent_replay}",
        (
            "Effective review ceiling: "
            f"{result.previous_effective_total} -> {result.new_effective_total}"
        ),
    ]
    if result.safe_next_action.command:
        lines.append(f"Next action: {result.safe_next_action.command}")
    else:
        lines.append(f"Next action: {result.safe_next_action.kind.value}")
    return "\n".join(lines) + "\n"


def render_recovery_prepare_output(result: RecoveryPrepareResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "recovery_id": result.recovery_id,
            "source_run_id": result.source_run_id,
            "state_kind": result.state_kind,
            "changed": result.changed,
            "idempotent_replay": result.idempotent_replay,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Prepared fresh-review recovery: {result.recovery_id}",
        f"Source run: {result.source_run_id}",
        f"State: {result.state_kind}",
        f"Changed: {result.changed}",
        f"Idempotent replay: {result.idempotent_replay}",
        f"Next action: {result.safe_next_action.command}",
    ]
    return "\n".join(lines) + "\n"


def render_recovery_start_output(result: RecoveryStartResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "recovery_id": result.recovery_id,
            "recovery_run_id": result.recovery_run_id,
            "source_run_id": result.source_run_id,
            "state_kind": result.state_kind,
            "changed": result.changed,
            "idempotent_replay": result.idempotent_replay,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Started fresh-review recovery: {result.recovery_id}",
        f"Recovery run: {result.recovery_run_id}",
        f"Source run: {result.source_run_id}",
        f"State: {result.state_kind}",
        f"Changed: {result.changed}",
        f"Idempotent replay: {result.idempotent_replay}",
        f"Next action: {result.safe_next_action.command}",
    ]
    return "\n".join(lines) + "\n"


def render_recovery_status_output(result: RecoveryStatusResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            **result.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Recovery: {result.recovery_id_prefix}",
        f"State: {result.state_kind}",
        f"Source run prefix: {result.source_run_id_prefix}",
    ]
    if result.recovery_run_id_prefix:
        lines.append(f"Recovery run prefix: {result.recovery_run_id_prefix}")
    if result.accepted_outcome:
        lines.append(f"Accepted outcome: {result.accepted_outcome}")
    lines.append(f"Next action: {result.safe_next_action.command}")
    return "\n".join(lines) + "\n"


def render_rollover_prepare_output(result: RolloverPrepareResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "rollover_id": result.rollover_id,
            "source_run_id": result.source_run_id,
            "state_kind": result.state_kind,
            "changed": result.changed,
            "idempotent_replay": result.idempotent_replay,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Prepared authenticated rollover: {result.rollover_id}",
        f"Source run: {result.source_run_id}",
        f"State: {result.state_kind}",
        f"Changed: {result.changed}",
        f"Idempotent replay: {result.idempotent_replay}",
        f"Next action: {result.safe_next_action.command}",
    ]
    return "\n".join(lines) + "\n"


def render_rollover_start_output(result: RolloverStartResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "rollover_id": result.rollover_id,
            "rollover_run_id": result.rollover_run_id,
            "source_run_id": result.source_run_id,
            "state_kind": result.state_kind,
            "changed": result.changed,
            "idempotent_replay": result.idempotent_replay,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Started authenticated rollover: {result.rollover_id}",
        f"Rollover run: {result.rollover_run_id}",
        f"Source run: {result.source_run_id}",
        f"State: {result.state_kind}",
        f"Changed: {result.changed}",
        f"Idempotent replay: {result.idempotent_replay}",
        f"Next action: {result.safe_next_action.command}",
    ]
    return "\n".join(lines) + "\n"


def render_rollover_status_output(result: RolloverStatusResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            **result.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Rollover: {result.rollover_id_prefix}",
        f"State: {result.state_kind}",
        f"Source run prefix: {result.source_run_id_prefix}",
    ]
    if result.rollover_run_id_prefix:
        lines.append(f"Rollover run prefix: {result.rollover_run_id_prefix}")
    if result.accepted_outcome:
        lines.append(f"Accepted outcome: {result.accepted_outcome}")
    lines.append(f"Next action: {result.safe_next_action.command}")
    return "\n".join(lines) + "\n"


def render_rollover_abort_output(result: RolloverAbortResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "rollover_id": result.rollover_id,
            "state_kind": result.state_kind,
            "changed": result.changed,
            "idempotent_replay": result.idempotent_replay,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Rollover abort: {result.rollover_id}",
        f"State: {result.state_kind}",
        f"Changed: {result.changed}",
        f"Idempotent replay: {result.idempotent_replay}",
        f"Next action: {result.safe_next_action.command}",
    ]
    return "\n".join(lines) + "\n"


def render_recovery_abort_output(result: RecoveryAbortResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "recovery_id": result.recovery_id,
            "state_kind": result.state_kind,
            "changed": result.changed,
            "idempotent_replay": result.idempotent_replay,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Recovery abort: {result.recovery_id}",
        f"State: {result.state_kind}",
        f"Changed: {result.changed}",
        f"Idempotent replay: {result.idempotent_replay}",
        f"Next action: {result.safe_next_action.command}",
    ]
    return "\n".join(lines) + "\n"


def render_review_retry_output(result: ReviewRetryResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "run_id": result.run_id,
            "source_run_id": result.source_run_id,
            "state_kind": result.state_kind,
            "changed": result.changed,
            "idempotent_replay": result.idempotent_replay,
            "recovery_successor": result.recovery_successor,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Review retry run: {result.run_id}",
        f"State: {result.state_kind}",
        f"Changed: {result.changed}",
        f"Idempotent replay: {result.idempotent_replay}",
    ]
    if result.source_run_id:
        lines.append(f"Source run: {result.source_run_id}")
    if result.recovery_successor:
        lines.append("Recovery successor: yes")
    lines.append(f"Next action: {result.safe_next_action.command}")
    return "\n".join(lines) + "\n"


def render_sequence_start_output(result: SequenceStartResult, *, output: str) -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "sequence_id": result.sequence_id,
            "run_id": result.run_id,
            "sequence_state_kind": result.sequence_state_kind,
            "run_state_kind": result.run_state_kind,
            "current_ordinal": result.current_ordinal,
            "entry_count": result.entry_count,
            "current_phase_name": result.current_phase_name,
            "changed": result.changed,
            "idempotent_replay": result.idempotent_replay,
            "safe_next_action": result.safe_next_action.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2) + "\n"
    header = (
        f"Started scheduler sequence {result.sequence_id} (reused existing authorization)"
        if result.idempotent_replay
        else f"Started scheduler sequence {result.sequence_id}"
    )
    lines = [
        header,
        f"Materialized run: {result.run_id}",
        f"Phase: {result.current_ordinal:02d}/{result.entry_count} ({result.current_phase_name})",
        f"Run state: {result.run_state_kind}",
        "Sequence start authorizes the frozen sequence and materializes only phase 1.",
        "Phase 20.2 does not commit checkpoints or materialize later phases.",
        f"Next action: {result.safe_next_action.command}",
    ]
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
    "SequencePrepareOptions",
    "SubmitOptions",
    "prepare_sequence",
    "render_cutover_cleanup_output",
    "render_scheduler_abort_output",
    "render_scheduler_history_output",
    "render_scheduler_timeline_output",
    "render_sequence_abort_output",
    "render_sequence_prepare_output",
    "render_sequence_start_output",
    "render_sequence_status_output",
    "scheduler_sequence_status",
    "scheduler_timeline",
    "render_list_output",
    "render_review_budget_extend_output",
    "render_recovery_abort_output",
    "render_recovery_prepare_output",
    "render_recovery_start_output",
    "render_recovery_status_output",
    "render_rollover_abort_output",
    "render_rollover_prepare_output",
    "render_rollover_start_output",
    "render_rollover_status_output",
    "render_review_retry_output",
    "abort_recovery",
    "abort_rollover",
    "prepare_recovery",
    "prepare_rollover",
    "recovery_status",
    "rollover_status",
    "start_recovery",
    "start_rollover",
    "scheduler_extend_review_budget",
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
    "scheduler_extend_review_budget",
    "scheduler_review_retry",
    "scheduler_status",
    "start_run",
    "submit_run",
]
