"""Typed Codex review result model and validation."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

TestsStatus = Literal[
    "passed",
    "failed",
    "skipped_findings_present",
    "blocked_environment",
    "not_applicable",
]
Severity = Literal["P0", "P1", "P2", "P3"]


class CodexReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_actionable_findings: bool
    findings_count: int = Field(ge=0)
    highest_severity: Severity | None
    review_markdown: str = Field(min_length=1)
    cursor_fix_prompt: str | None
    tests_status: TestsStatus
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cross_fields(self) -> Self:
        if self.has_actionable_findings:
            if self.findings_count <= 0:
                raise ValueError("findings_count must be > 0 when has_actionable_findings is true")
            if self.highest_severity is None:
                raise ValueError("highest_severity is required when findings exist")
            if not self.cursor_fix_prompt or not self.cursor_fix_prompt.strip():
                raise ValueError("cursor_fix_prompt is required when findings exist")
        else:
            if self.findings_count != 0:
                raise ValueError("findings_count must be 0 when has_actionable_findings is false")
            if self.highest_severity is not None:
                raise ValueError("highest_severity must be null when there are no findings")
            if self.cursor_fix_prompt is not None:
                raise ValueError("cursor_fix_prompt must be null when there are no findings")
        return self


def completion_status_for_review(result: CodexReviewResult) -> str:
    if result.has_actionable_findings:
        return "waiting_for_cursor_fix"
    if result.tests_status in {"failed", "blocked_environment", "skipped_findings_present"}:
        return "completed_with_residual_risk"
    return "completed"
