"""Read-only status command."""

from __future__ import annotations

import json
from pathlib import Path

from ai_dev_loop.abort_control import abort_control_summary
from ai_dev_loop.config import format_codex_override
from ai_dev_loop.fresh_codex_reviewer import is_fresh_codex_reviewer_run
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.runners.staging import staging_complete_for_iteration
from ai_dev_loop.state import RunState, shorten_session_id


def render_status(run_id: str, *, output: str = "text") -> str:
    run_path, state = load_run(run_id)
    next_action = _next_action(state, run_path)
    control = abort_control_summary(run_path)
    if output == "json":
        payload = {
            "schema_version": 1,
            "run_id": state.run_id,
            "status": state.status.value,
            "project": state.project.name,
            "repository": state.repository.root,
            "branch": state.repository.branch,
            "initial_head": state.repository.initial_head,
            "current_review_iteration": state.workflow.current_review_iteration,
            "max_review_iterations": state.workflow.max_review_iterations,
            "cursor_chat_id": state.cursor.chat_id,
            "codex_session_id": state.codex.session_id,
            "codex_session_model": state.codex.session_model,
            "codex_session_reasoning_effort": state.codex.session_reasoning_effort,
            "codex_review_model": state.codex.review_model,
            "codex_review_reasoning_effort": state.codex.review_reasoning_effort,
            "codex_review_model_source": state.codex.review_model_source,
            "codex_review_reasoning_source": state.codex.review_reasoning_source,
            "codex_model_family_warning": state.codex.model_family_warning,
            "last_error": state.last_error,
            "result": state.result,
            "run_directory": str(run_path),
            "next_safe_action": next_action,
            "iteration_count": len(state.iterations),
            "abort_control": control,
            "recovery": None
            if state.recovery is None
            else {
                "source_run_id": state.recovery.source_run_id,
                "source_status": state.recovery.source_status,
                "source_iteration": state.recovery.source_iteration,
                "recovered_checkpoint": state.recovery.recovered_checkpoint,
                "runtime_migration": state.recovery.runtime_migration,
                "reason_code": state.recovery.reason_code,
                "cursor_output_fingerprint_sha256": (
                    state.recovery.cursor_output_fingerprint_sha256
                ),
                "legacy_cursor_output_adopted": state.recovery.legacy_cursor_output_adopted,
                "legacy_cursor_usage_limit_adopted": state.recovery.legacy_cursor_usage_limit_adopted,
                "source_cursor_model": state.recovery.source_cursor_model,
                "cursor_model_fallback": state.recovery.cursor_model_fallback,
                "continuation_envelope_path": state.recovery.continuation_envelope_path,
                "usage_limit_fingerprint_sha256": state.recovery.usage_limit_fingerprint_sha256,
            },
        }
        return json.dumps(payload, indent=2) + "\n"

    lines = [
        f"Run: {state.run_id}",
        f"Status: {state.status.value}",
        f"Project: {state.project.name}",
        f"Repository: {state.repository.root}",
        f"Branch: {state.repository.branch}",
        f"Initial HEAD: {state.repository.initial_head}",
        f"Review iteration: {state.workflow.current_review_iteration}/{state.workflow.max_review_iterations}",
        f"Recorded iterations: {len(state.iterations)}",
        f"Cursor chat: {state.cursor.chat_id or '(not created)'}",
        f"Codex session: {shorten_session_id(state.codex.session_id) if state.codex.session_id else '(unbound)'}",
        f"Session model: {state.codex.session_model or '(unset)'}",
        f"Session reasoning: {state.codex.session_reasoning_effort or '(unset)'}",
        (
            f"Codex review model: {format_codex_override(state.codex.review_model)}"
            + (f" ({state.codex.review_model_source})" if state.codex.review_model_source else "")
        ),
        (
            f"Codex review reasoning: {format_codex_override(state.codex.review_reasoning_effort)}"
            + (
                f" ({state.codex.review_reasoning_source})"
                if state.codex.review_reasoning_source
                else ""
            )
        ),
        f"Run directory: {run_path}",
        f"Next safe action: {next_action}",
    ]
    if state.recovery is not None:
        lines.insert(
            2,
            (
                f"Recovery successor of: {state.recovery.source_run_id} "
                f"(checkpoint={state.recovery.recovered_checkpoint}, "
                f"iteration={state.recovery.source_iteration})"
            ),
        )
    if state.codex.model_family_warning:
        lines.append(f"Model family warning: {state.codex.model_family_warning}")
    if control["abort_requested"]:
        lines.append("Abort request: pending")
    if control["active_process_registered"]:
        component = control.get("active_component", "unknown")
        iteration = control.get("active_iteration", "?")
        lines.append(f"Active child process: {component} iteration {iteration}")
    if state.last_error:
        lines.append(f"Last error: {state.last_error}")
    if state.result:
        lines.append(f"Result: {state.result}")
    return "\n".join(lines) + "\n"


def _next_action(state: RunState, run_path: Path) -> str:
    status = state.status.value
    if status == "prepared":
        if state.controller is not None:
            if is_fresh_codex_reviewer_run(state.codex):
                return (
                    "From the controller session run ai_dev_loop launch <run-id> "
                    "--controller-session-id <exact-id>. Reviewer B is created at the "
                    "first review boundary."
                )
            return (
                "This run uses the retired session-bound A/B contract. Inspect only, "
                "then prepare a fresh run with explicit --codex-review-model and "
                "--codex-review-reasoning-effort."
            )
        return "Exit Codex TUI, then run ai_dev_loop start <run-id>."
    if status == "staging":
        iteration = f"{state.workflow.current_review_iteration:02d}"
        if state.workflow.current_review_iteration > 0 and staging_complete_for_iteration(
            state, run_path, iteration
        ):
            return "Run ai_dev_loop resume <run-id> to continue with Codex review."
        return (
            "Cursor execution finished but Git staging is incomplete. "
            "Inspect git/status/ and cursor/iterations/ artifacts, then run ai_dev_loop resume <run-id>."
        )
    if status == "reviewing":
        return "Run ai_dev_loop resume <run-id> to continue or finish Codex review processing."
    if status == "waiting_for_cursor_fix":
        return "Run ai_dev_loop resume <run-id> to send the stored fix prompt to Cursor."
    if status == "completed":
        return (
            "Run completed with no actionable findings. Changes remain staged in the "
            "target repository."
        )
    if status == "completed_with_residual_risk":
        return (
            "Run completed with residual risk. Inspect codex/reviews/ and repository state "
            "before committing."
        )
    if status == "max_iterations_reached":
        resume_command = f"ai_dev_loop resume {state.run_id}"
        if state.controller is not None:
            resume_command = (
                f"ai_dev_loop launch {state.run_id} "
                "--controller-session-id <exact-controller-session-id>"
            )
        return (
            "Maximum review iterations reached. To continue the same Cursor chat and reviewer "
            "session, explicitly extend the budget with "
            f"ai_dev_loop extend {state.run_id} --additional-review-iterations <positive-int>, "
            f"then {resume_command}."
        )
    if status in {"running_cursor", "validating"}:
        return "Wait for start/resume to finish or inspect logs if the run appears stuck."
    if status == "interrupted":
        if state.recovery is not None:
            return (
                "Recovery successor ready. Run ai_dev_loop resume <run-id> "
                "(add --update-tools only if tool compatibility requires it)."
            )
        return "Run ai_dev_loop resume <run-id> after inspecting cursor/ and codex/ artifacts."
    if status == "failed":
        # Prefer structured usage-limit evidence over last_error prose.
        from ai_dev_loop.recovery_planner import analyze_recovery

        try:
            analysis = analyze_recovery(state, run_path, resolve_runtime=False)
        except Exception:
            analysis = None
        if (
            analysis is not None
            and analysis.checkpoint == "cursor"
            and analysis.reason_code == "cursor_usage_limit"
        ):
            if analysis.legacy_cursor_usage_limit_adopted or (
                not analysis.eligible
                and "legacy_cursor_usage_limit_requires_explicit_adoption" in analysis.warnings
            ):
                return (
                    "Historical Cursor usage-limit failure lacks a contemporaneous fingerprint. "
                    f"After confirming the repository was not modified since Cursor stopped, run "
                    f"ai_dev_loop recover {state.run_id} --dry-run "
                    "--adopt-current-cursor-output --cursor-model auto, then recover and resume."
                )
            return (
                "Cursor reached the usage limit for its configured model. "
                f"Recover with ai_dev_loop recover {state.run_id} --cursor-model auto, "
                "then ai_dev_loop resume <recovery-run-id>."
            )
        return (
            "Inspect last_error and artifacts. Eligible failed runs may be recovered with "
            "ai_dev_loop recover --dry-run <run-id> (add --adopt-current-cursor-output for "
            "historical staging failures missing a post-Cursor fingerprint; add "
            "--cursor-model auto for Cursor usage-limit failures), then "
            "ai_dev_loop recover <run-id> and ai_dev_loop resume <recovery-run-id>."
        )
    if status == "aborted":
        return (
            "Run was aborted. Inspect cursor/, codex/, and git/ artifacts. "
            "Repository contents and staged changes were preserved."
        )
    return "Inspect artifacts manually."
