"""Narrow classification of Codex review failures for scheduler recovery."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ai_dev_loop.response_schema import events_indicate_usage_limit_exceeded

FAILURE_CODE_CODEX_USAGE_LIMIT = "codex_usage_limit"
SAFE_CODEX_USAGE_LIMIT_SUMMARY = "Codex reached the account usage limit for review."


class CodexFailureCode(StrEnum):
    USAGE_LIMIT = FAILURE_CODE_CODEX_USAGE_LIMIT
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CodexFailureClassification:
    code: CodexFailureCode
    safe_summary: str | None = None

    @property
    def is_usage_limit(self) -> bool:
        return self.code == CodexFailureCode.USAGE_LIMIT


def classify_codex_review_events_text(text: str) -> CodexFailureClassification:
    """Classify captured Codex review JSONL events for usage-limit eligibility."""

    from ai_dev_loop.response_schema import events_text_indicates_usage_limit_exceeded

    if events_text_indicates_usage_limit_exceeded(text):
        return CodexFailureClassification(
            code=CodexFailureCode.USAGE_LIMIT,
            safe_summary=SAFE_CODEX_USAGE_LIMIT_SUMMARY,
        )
    return CodexFailureClassification(code=CodexFailureCode.UNKNOWN)


def is_codex_usage_limit_recovery_eligible(outcome: dict[str, object]) -> bool:
    """True only when a typed usage-limit marker is the sole recoverable failure."""

    if str(outcome.get("failure_code", "")).strip() != FAILURE_CODE_CODEX_USAGE_LIMIT:
        return False
    if outcome.get("timed_out"):
        return False
    if outcome.get("stdout_truncated") or outcome.get("stderr_truncated"):
        return False
    if outcome.get("review_output_truncated"):
        return False
    if str(outcome.get("review_block_reason", "")).strip():
        return False
    if str(outcome.get("bootstrap_uncertainty_reason", "")).strip():
        return False
    if str(outcome.get("failure_kind", "")).strip():
        return False
    return outcome.get("parse_ok") is not False


def classify_codex_review_events_path(events_path: Path) -> CodexFailureClassification:
    """Classify a persisted Codex review events artifact."""

    if events_indicate_usage_limit_exceeded(events_path):
        return CodexFailureClassification(
            code=CodexFailureCode.USAGE_LIMIT,
            safe_summary=SAFE_CODEX_USAGE_LIMIT_SUMMARY,
        )
    return CodexFailureClassification(code=CodexFailureCode.UNKNOWN)
