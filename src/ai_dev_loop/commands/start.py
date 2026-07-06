"""Start command: preflight, locks, and bounded workflow execution."""

from __future__ import annotations

from ai_dev_loop.workflow_engine import WorkflowResult, render_workflow_output, start_run

__all__ = ["CODEX_TUI_WARNING", "StartResult", "WorkflowResult", "render_start_output", "start_run"]

CODEX_TUI_WARNING = "Important: exit the active Codex TUI before continuing with start."

# Backward-compatible alias for tests importing StartResult.
StartResult = WorkflowResult


def render_start_output(result: WorkflowResult) -> str:
    return render_workflow_output(result)
