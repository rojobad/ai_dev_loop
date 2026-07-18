"""Typed Codex result for GitHub PR thread adjudication."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_dev_loop.review_result import Severity, TestsStatus

ThreadDecision = Literal["actionable", "not_applicable", "uncertain"]


class GithubThreadDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1)
    decision: ThreadDecision
    inline_reply: str | None = None
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_reply_shape(self) -> Self:
        if self.decision == "actionable":
            if self.inline_reply is not None:
                raise ValueError("inline_reply must be null for actionable threads")
        else:
            if not self.inline_reply or not self.inline_reply.strip():
                raise ValueError(
                    "inline_reply is required for not_applicable and uncertain decisions"
                )
            stripped = self.inline_reply.lstrip()
            if not stripped.startswith("@rojobad"):
                raise ValueError("inline_reply must begin with @rojobad")
        return self


class GithubPrReviewResult(BaseModel):
    """Schema-constrained Codex output for one external-feedback adjudication turn."""

    model_config = ConfigDict(extra="forbid")

    eligible_thread_ids: list[str] = Field(min_length=1)
    thread_decisions: list[GithubThreadDecision] = Field(min_length=1)
    all_actionable: bool
    review_markdown: str = Field(min_length=1)
    cursor_fix_prompt: str | None
    tests_status: TestsStatus
    summary: str = Field(min_length=1)
    residual_risk_comment: str | None = None
    highest_severity: Severity | None = None

    @model_validator(mode="after")
    def validate_cross_fields(self) -> Self:
        decision_ids = [item.thread_id for item in self.thread_decisions]
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("thread_decisions must have unique thread_id values")
        eligible = list(self.eligible_thread_ids)
        if len(eligible) != len(set(eligible)):
            raise ValueError("eligible_thread_ids must be unique")
        if set(decision_ids) != set(eligible):
            raise ValueError("thread_decisions must cover exactly the eligible_thread_ids set")
        actionable = all(item.decision == "actionable" for item in self.thread_decisions)
        if self.all_actionable != actionable:
            raise ValueError("all_actionable must match per-thread decisions")
        if actionable:
            if not self.cursor_fix_prompt or not self.cursor_fix_prompt.strip():
                raise ValueError("cursor_fix_prompt is required when all threads are actionable")
            if any(item.inline_reply is not None for item in self.thread_decisions):
                raise ValueError("inline_reply must be null when all threads are actionable")
        else:
            if self.cursor_fix_prompt is not None:
                raise ValueError("cursor_fix_prompt must be null when any thread is not actionable")
            non_actionable = [
                item for item in self.thread_decisions if item.decision != "actionable"
            ]
            if not non_actionable:
                raise ValueError("expected at least one non-actionable decision")
        return self
