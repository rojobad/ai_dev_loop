"""Recover failed GitHub PR-review adjudication runs without re-posting a trigger."""

from __future__ import annotations

import json
import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_dev_loop.commands.pr_review import (
    WORKER_LAUNCHER_REL,
    _copy_identity_artifacts,
    _create_successor_layout,
    _run_locks,
)
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.event_log import append_orchestrator_event
from ai_dev_loop.integrations.codex.session_runtime import require_codex_session_id
from ai_dev_loop.paths import ensure_dir, run_dir, runs_dir, set_sensitive_file_mode
from ai_dev_loop.response_schema import events_indicate_adjudication_schema_rejection
from ai_dev_loop.resume_planner import TERMINAL_STATUSES
from ai_dev_loop.run_discovery import list_run_directories, load_run
from ai_dev_loop.runners.github import get_pull_request
from ai_dev_loop.state import (
    RecoveryState,
    RunState,
    RunStatus,
    atomic_write_json,
    generate_run_id,
    save_run_state,
    utc_now,
)


@dataclass
class PrReviewRecoveryAnalysis:
    source_run_id: str
    eligible: bool = False
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checkpoint: str | None = None
    reason_code: str | None = None
    cycle_number: int | None = None
    pr_number: int | None = None
    bound_head_sha_prefix: str | None = None
    expected_eligible_thread_ids: list[str] = field(default_factory=list)
    expected_thread_count: int = 0
    existing_successor_run_id: str | None = None
    existing_successor_status: str | None = None
    reused_existing_successor: bool = False
    has_controller: bool = False
    # Validated controller A UUID used only to render an executable resume command.
    # Never copied into public events or pr-review/status JSON fields.
    controller_session_id: str | None = None
    trigger_present: bool = False
    historical_schema_rejection: bool = False


@dataclass(frozen=True)
class PrReviewRecoveryResult:
    source_run_id: str
    recovery_run_id: str | None
    dry_run: bool
    analysis: PrReviewRecoveryAnalysis
    message: str
    resume_command: str | None


def _active_worker_pid(run_directory: Path) -> int | None:
    launcher = run_directory / WORKER_LAUNCHER_REL
    if not launcher.is_file():
        return None
    try:
        payload = json.loads(launcher.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _cycle_events_path(run_directory: Path, cycle_number: int) -> Path:
    return run_directory / f"github/cycles/{cycle_number:02d}/codex.events.jsonl"


def _has_side_effects(gpr: Any, state: RunState) -> list[str]:
    """Block recovery when adjudication already produced GitHub/Cursor side effects.

    Initial PR publish + trigger for cycle 1 are expected and are not blockers.
    """

    blockers: list[str] = []
    if gpr.processed_thread_ids:
        blockers.append("processed_threads_present")
    if gpr.replied_thread_ids:
        blockers.append("replied_threads_present")
    if gpr.resolved_thread_ids:
        blockers.append("resolved_threads_present")
    if gpr.last_external_result_path:
        blockers.append("external_result_present")
    if gpr.external_fix_prompt_path:
        blockers.append("external_fix_prompt_present")
    if gpr.continue_comment_id:
        blockers.append("continue_comment_present")
    if state.iterations:
        blockers.append("cursor_iterations_present")
    # Independent cycles must not have created Cursor yet; source-run cycles reuse chat.
    if gpr.origin == "independent_pr" and state.cursor.chat_id:
        blockers.append("cursor_chat_already_created")
    if gpr.lifecycle in {
        "fixing_external_feedback",
        "publishing_external_fix",
        "waiting_for_user_attention",
        "completed",
        "max_external_cycles_reached",
    }:
        blockers.append(f"lifecycle_past_adjudication:{gpr.lifecycle}")
    return blockers


def _classify_schema_incompatible_source(
    state: RunState,
    run_directory: Path,
) -> tuple[bool, bool]:
    """Return (eligible_classification, historical_artifact_match)."""

    gpr = state.github_pr_review
    assert gpr is not None
    if gpr.worker_outcome == "adjudication_schema_incompatible":
        return True, False
    events = _cycle_events_path(run_directory, gpr.cycle_number)
    if events_indicate_adjudication_schema_rejection(events):
        return True, True
    return False, False


def analyze_pr_review_recovery(
    run_id: str,
    *,
    verify_remote: bool = True,
) -> PrReviewRecoveryAnalysis:
    run_directory, state = load_run(run_id)
    analysis = PrReviewRecoveryAnalysis(source_run_id=state.run_id)
    gpr = state.github_pr_review
    if gpr is None:
        analysis.blockers.append("not_a_pr_review_cycle")
        return analysis
    analysis.cycle_number = gpr.cycle_number
    analysis.pr_number = gpr.pr_number
    analysis.bound_head_sha_prefix = gpr.bound_head_sha[:12]
    analysis.has_controller = state.controller is not None
    analysis.trigger_present = bool(gpr.request_comment_id and gpr.request_marker)
    analysis.checkpoint = "external_adjudication"
    analysis.reason_code = "github_adjudication_schema_incompatible"

    if state.controller is not None:
        try:
            analysis.controller_session_id = require_codex_session_id(
                state.controller.controller_session_id
            )
        except ValidationError:
            analysis.blockers.append("controller_session_invalid")
            analysis.controller_session_id = None
        else:
            if analysis.controller_session_id == state.codex.session_id:
                analysis.blockers.append("controller_equals_reviewer")

    if state.status != RunStatus.FAILED:
        analysis.blockers.append("source_not_failed")
    classified, historical = _classify_schema_incompatible_source(state, run_directory)
    analysis.historical_schema_rejection = historical
    if not classified:
        analysis.blockers.append("not_adjudication_schema_incompatible")

    if gpr.pr_number is None:
        analysis.blockers.append("pr_not_bound")
    if not analysis.trigger_present:
        analysis.blockers.append("trigger_missing")

    expected = list(gpr.expected_eligible_thread_ids or gpr.eligible_thread_ids)
    if not expected:
        analysis.blockers.append("eligible_threads_missing")
    else:
        analysis.expected_eligible_thread_ids = expected
        analysis.expected_thread_count = len(expected)

    analysis.blockers.extend(_has_side_effects(gpr, state))

    if _active_worker_pid(run_directory) is not None:
        analysis.blockers.append("active_worker_present")

    if (
        verify_remote
        and gpr.pr_number is not None
        and "not_a_pr_review_cycle" not in analysis.blockers
    ):
        try:
            from ai_dev_loop.commands.pr_review import _require_github_config

            config = _require_github_config(Path(state.repository.root))
            assert config.github is not None
            pr = get_pull_request(
                config.github.command,
                cwd=state.repository.root,
                pr_number=gpr.pr_number,
            )
            if pr.state != "OPEN":
                analysis.blockers.append("pr_not_open")
            if pr.head_sha != gpr.bound_head_sha:
                analysis.blockers.append("head_sha_drift")
        except Exception as exc:
            analysis.blockers.append(f"remote_pr_unreadable:{type(exc).__name__}")

    analysis.eligible = len(analysis.blockers) == 0
    _attach_matching_successor(analysis, state)
    return analysis


def _attach_matching_successor(analysis: PrReviewRecoveryAnalysis, source: RunState) -> None:
    matches: list[tuple[Path, RunState]] = []
    expected = set(analysis.expected_eligible_thread_ids)
    for path, candidate in list_run_directories():
        recovery = candidate.recovery
        if recovery is None:
            continue
        if recovery.source_run_id != source.run_id:
            continue
        if recovery.recovered_checkpoint != "external_adjudication":
            continue
        if recovery.reason_code != "github_adjudication_schema_incompatible":
            continue
        if set(recovery.expected_eligible_thread_ids or []) != expected:
            continue
        if candidate.github_pr_review is None:
            continue
        if candidate.github_pr_review.cycle_number != analysis.cycle_number:
            continue
        if candidate.github_pr_review.pr_number != analysis.pr_number:
            continue
        if candidate.github_pr_review.bound_head_sha[:12] != analysis.bound_head_sha_prefix:
            continue
        matches.append((path, candidate))
    if not matches:
        return
    if len(matches) > 1:
        ids = ", ".join(item.run_id for _, item in matches)
        analysis.warnings.append(f"multiple_matching_successors:{ids}")
        return
    _path, successor = matches[0]
    analysis.existing_successor_run_id = successor.run_id
    analysis.existing_successor_status = successor.status.value
    if not analysis.eligible:
        return
    if successor.status not in TERMINAL_STATUSES:
        analysis.reused_existing_successor = True


def _create_recovery_successor(
    *,
    source_dir: Path,
    source: RunState,
    analysis: PrReviewRecoveryAnalysis,
) -> str:
    assert analysis.cycle_number is not None
    assert analysis.expected_eligible_thread_ids
    gpr = source.github_pr_review
    assert gpr is not None

    now = utc_now()
    project = source.project.name
    recovery_run_id = generate_run_id(project, now=now)
    final_dir = run_dir(project, recovery_run_id)
    if final_dir.exists():
        raise ValidationError(f"recovery run directory already exists: {final_dir}")

    project_root = runs_dir() / project
    ensure_dir(project_root)
    temp_dir = project_root / f".pr-review-recover-{recovery_run_id}-{secrets.token_hex(4)}"
    try:
        _create_successor_layout(temp_dir)
        _copy_identity_artifacts(source_dir, temp_dir)
        # Audit-only copy of the failed adjudication events (no result body).
        events_src = _cycle_events_path(source_dir, analysis.cycle_number)
        if events_src.is_file():
            events_dest = _cycle_events_path(temp_dir, analysis.cycle_number)
            events_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(events_src, events_dest)
            set_sensitive_file_mode(events_dest)

        expected = list(analysis.expected_eligible_thread_ids)
        recovery = RecoveryState(
            source_run_id=source.run_id,
            source_status=RunStatus.FAILED.value,
            source_iteration=analysis.cycle_number,
            recovered_checkpoint="external_adjudication",
            source_staged_patch_sha256=None,
            created_at=now,
            runtime_migration="none",
            reason_code="github_adjudication_schema_incompatible",
            expected_eligible_thread_ids=expected,
        )
        successor = source.model_copy(deep=True)
        successor.run_id = recovery_run_id
        successor.created_at = now
        successor.updated_at = now
        successor.status = RunStatus.INTERRUPTED
        successor.result = (
            f"Recovered GitHub adjudication from failed run {source.run_id} "
            "without re-posting @codex review."
        )
        successor.last_error = None
        successor.iterations = []
        successor.recovery = recovery
        successor.github_pr_review = gpr.model_copy(
            update={
                "lifecycle": "interrupted",
                "worker_outcome": None,
                "eligible_thread_ids": expected,
                "expected_eligible_thread_ids": expected,
                "processed_thread_ids": [],
                "replied_thread_ids": [],
                "resolved_thread_ids": [],
                "last_external_result_path": None,
                "external_fix_prompt_path": None,
                "continue_comment_id": None,
            }
        )
        assert successor.codex.session_id == source.codex.session_id
        if source.controller is not None:
            assert successor.controller is not None
            assert (
                successor.controller.controller_session_id
                == source.controller.controller_session_id
            )
        if gpr.origin == "source_run":
            assert successor.cursor.chat_id == source.cursor.chat_id
        else:
            successor.cursor = successor.cursor.model_copy(update={"chat_id": None})

        save_run_state(temp_dir, successor)
        atomic_write_json(
            temp_dir / "manifest.json",
            {
                "schema_version": 1,
                "run_id": recovery_run_id,
                "project": project,
                "created_at": now.isoformat(),
                "artifacts": [],
                "recovery": {
                    "source_run_id": source.run_id,
                    "recovered_checkpoint": "external_adjudication",
                    "reason_code": "github_adjudication_schema_incompatible",
                    "expected_thread_count": len(expected),
                },
            },
            sensitive=True,
        )
        append_orchestrator_event(
            temp_dir,
            run_id=recovery_run_id,
            component="orchestrator",
            event="pr_review_adjudication_recovery_successor_created",
            status=successor.status.value,
            detail={
                "source_run_id": source.run_id,
                "checkpoint": "external_adjudication",
                "reason_code": "github_adjudication_schema_incompatible",
                "expected_thread_count": len(expected),
                "pr_number": gpr.pr_number,
                "bound_head_sha_prefix": gpr.bound_head_sha[:12],
                "trigger_reposted": False,
            },
        )
        if final_dir.exists():
            raise ValidationError(f"recovery run directory already exists: {final_dir}")
        temp_dir.rename(final_dir)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return recovery_run_id


def recover_pr_review_cycle(run_id: str, *, dry_run: bool = False) -> PrReviewRecoveryResult:
    """Create or reuse an immutable adjudication-recovery successor for a failed cycle."""

    run_directory, source = load_run(run_id)
    analysis = analyze_pr_review_recovery(run_id, verify_remote=True)

    if dry_run:
        successor_token = analysis.existing_successor_run_id or "<successor-run-id>"
        resume_hint = (
            _resume_command_for(analysis, successor_run_id=successor_token)
            if analysis.eligible
            else None
        )
        message = (
            "Dry-run only: no successor created, no GitHub writes, trigger will not be re-posted."
            if analysis.eligible
            else f"Dry-run: run is not recoverable ({', '.join(analysis.blockers)})."
        )
        return PrReviewRecoveryResult(
            source_run_id=source.run_id,
            recovery_run_id=None,
            dry_run=True,
            analysis=analysis,
            message=message,
            resume_command=resume_hint,
        )

    if not analysis.eligible:
        raise ValidationError(
            "pr-review recover rejected: " + ", ".join(analysis.blockers or ["not_eligible"])
        )

    if analysis.reused_existing_successor and analysis.existing_successor_run_id:
        resume_command = _resume_command_for(
            analysis, successor_run_id=analysis.existing_successor_run_id
        )
        return PrReviewRecoveryResult(
            source_run_id=source.run_id,
            recovery_run_id=analysis.existing_successor_run_id,
            dry_run=False,
            analysis=analysis,
            message=(
                f"Reused existing adjudication recovery successor "
                f"{analysis.existing_successor_run_id}; trigger will not be re-posted."
            ),
            resume_command=resume_command,
        )

    locks = _run_locks(
        run_directory,
        run_id=source.run_id,
        repository_path=source.repository.root,
    )
    locks.acquire()
    try:
        # Re-validate under lock; source must remain failed/immutable.
        fresh = analyze_pr_review_recovery(run_id, verify_remote=True)
        if not fresh.eligible:
            raise ValidationError(
                "pr-review recover rejected after revalidation: " + ", ".join(fresh.blockers)
            )
        reused_id = fresh.existing_successor_run_id
        if fresh.reused_existing_successor and reused_id is not None:
            analysis = fresh
            resume_command = _resume_command_for(analysis, successor_run_id=reused_id)
            return PrReviewRecoveryResult(
                source_run_id=source.run_id,
                recovery_run_id=reused_id,
                dry_run=False,
                analysis=analysis,
                message=(
                    f"Reused existing adjudication recovery successor "
                    f"{reused_id}; trigger will not be re-posted."
                ),
                resume_command=resume_command,
            )
        recovery_run_id = _create_recovery_successor(
            source_dir=run_directory,
            source=source,
            analysis=fresh,
        )
        analysis = fresh
    finally:
        locks.release()

    # Source stays failed; touch only to prove immutability was preserved.
    source_after = load_run(run_id)[1]
    if source_after.status != RunStatus.FAILED:
        raise ValidationError("source run status changed unexpectedly during recovery")

    resume_command = _resume_command_for(analysis, successor_run_id=recovery_run_id)
    return PrReviewRecoveryResult(
        source_run_id=source.run_id,
        recovery_run_id=recovery_run_id,
        dry_run=False,
        analysis=analysis,
        message=(
            f"Created adjudication recovery successor {recovery_run_id} from "
            f"{source.run_id}. Source remains failed. Trigger will not be re-posted."
        ),
        resume_command=resume_command,
    )


def _resume_command_for(analysis: PrReviewRecoveryAnalysis, *, successor_run_id: str) -> str:
    if analysis.has_controller:
        controller_id = analysis.controller_session_id
        if not controller_id:
            raise ValidationError(
                "A/B recovery resume command requires a validated controller session id"
            )
        return (
            f"ai_dev_loop pr-review resume {successor_run_id} "
            f"--controller-session-id {controller_id}"
        )
    return f"ai_dev_loop pr-review resume {successor_run_id}"


def render_pr_review_recovery_analysis(
    result: PrReviewRecoveryResult,
    *,
    output: str = "text",
) -> str:
    analysis = result.analysis
    payload = {
        "schema_version": 1,
        "dry_run": result.dry_run,
        "source_run_id": result.source_run_id,
        "eligible": analysis.eligible,
        "blockers": analysis.blockers,
        "warnings": analysis.warnings,
        "checkpoint": analysis.checkpoint,
        "reason_code": analysis.reason_code,
        "cycle_number": analysis.cycle_number,
        "pr_number": analysis.pr_number,
        "bound_head_sha_prefix": analysis.bound_head_sha_prefix,
        "expected_thread_count": analysis.expected_thread_count,
        "historical_schema_rejection": analysis.historical_schema_rejection,
        "existing_successor_run_id": analysis.existing_successor_run_id,
        "trigger_will_be_reposted": False,
        "resume_command": result.resume_command,
        "message": result.message,
    }
    if output == "json":
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Source: {result.source_run_id}",
        f"Eligible: {analysis.eligible}",
        f"Checkpoint: {analysis.checkpoint}",
        f"Reason: {analysis.reason_code}",
        f"Expected threads: {analysis.expected_thread_count}",
        "Trigger will be re-posted: false",
        result.message,
    ]
    if analysis.blockers:
        lines.append("Blockers: " + ", ".join(analysis.blockers))
    if result.resume_command:
        lines.append(f"Next: {result.resume_command}")
    return "\n".join(lines) + "\n"


def render_pr_review_recovery_result(
    result: PrReviewRecoveryResult,
    *,
    output: str = "text",
) -> str:
    analysis = result.analysis
    payload = {
        "schema_version": 1,
        "dry_run": result.dry_run,
        "source_run_id": result.source_run_id,
        "recovery_run_id": result.recovery_run_id,
        "reused_existing_successor": analysis.reused_existing_successor,
        "checkpoint": analysis.checkpoint,
        "reason_code": analysis.reason_code,
        "pr_number": analysis.pr_number,
        "bound_head_sha_prefix": analysis.bound_head_sha_prefix,
        "expected_thread_count": analysis.expected_thread_count,
        "trigger_will_be_reposted": False,
        "resume_command": result.resume_command,
        "message": result.message,
    }
    if output == "json":
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        result.message,
        f"Source remains failed: {result.source_run_id}",
        "Trigger will be re-posted: false",
    ]
    if result.recovery_run_id:
        lines.append(f"Successor: {result.recovery_run_id}")
    if result.resume_command:
        lines.append(f"Next: {result.resume_command}")
    return "\n".join(lines) + "\n"
