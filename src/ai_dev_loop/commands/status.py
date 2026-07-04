"""Read-only status command."""

from __future__ import annotations

import json

from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import shorten_session_id


def render_status(run_id: str, *, output: str = "text") -> str:
    run_path, state = load_run(run_id)
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
            "last_error": state.last_error,
            "run_directory": str(run_path),
            "next_safe_action": _next_action(state.status.value),
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
        f"Cursor chat: {state.cursor.chat_id or '(not created)'}",
        f"Codex session: {shorten_session_id(state.codex.session_id)}",
        f"Run directory: {run_path}",
        f"Next safe action: {_next_action(state.status.value)}",
    ]
    if state.last_error:
        lines.append(f"Last error: {state.last_error}")
    return "\n".join(lines) + "\n"


def _next_action(status: str) -> str:
    if status == "prepared":
        return "Exit Codex TUI, then run ai_dev_loop start <run-id> (not implemented in Phase 1)."
    return "Inspect artifacts or wait for a later-phase recovery command."
