"""Resume command: continue runs from durable checkpoints."""

from __future__ import annotations

from ai_dev_loop.workflow_engine import WorkflowResult, render_workflow_output, resume_run

__all__ = ["ResumeResult", "WorkflowResult", "render_resume_output", "resume_run"]

ResumeResult = WorkflowResult


def render_resume_output(result: WorkflowResult) -> str:
    return render_workflow_output(result)
