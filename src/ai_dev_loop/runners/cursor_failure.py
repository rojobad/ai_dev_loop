"""Narrow classification of Cursor process failures for recovery eligibility."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

FAILURE_CODE_USAGE_LIMIT = "cursor_usage_limit"
SAFE_USAGE_LIMIT_SUMMARY = "Cursor reached the usage limit for its configured model."

# Require the typed ActionRequiredError marker plus both usage-limit and
# model-switch signals. Generic words like "limit" alone must not match.
_ACTION_REQUIRED = re.compile(r"actionrequirederror", re.IGNORECASE)
_USAGE_LIMIT = re.compile(
    r"(?:monthly\s+)?usage\s+limit|reached\s+your\s+usage\s+limit|"
    r"usage\s+limit\s+for\s+(?:this|your|the)\s+model",
    re.IGNORECASE,
)
_SWITCH_MODEL = re.compile(
    r"switch\s+(?:to\s+)?(?:a\s+)?(?:different\s+)?model|"
    r"switch\s+to\s+auto|"
    r"try\s+(?:a\s+)?(?:different\s+)?model|"
    r"change\s+(?:your\s+)?model",
    re.IGNORECASE,
)


class CursorFailureCode(StrEnum):
    USAGE_LIMIT = FAILURE_CODE_USAGE_LIMIT
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CursorFailureClassification:
    code: CursorFailureCode
    safe_summary: str | None = None

    @property
    def is_usage_limit(self) -> bool:
        return self.code == CursorFailureCode.USAGE_LIMIT


def classify_cursor_failure_text(
    *,
    returncode: int | None,
    timed_out: bool,
    stderr: str = "",
    structured_errors: Iterable[str] = (),
) -> CursorFailureClassification:
    """Classify a completed Cursor subprocess for recovery eligibility.

    Only nonzero exits with the specific ActionRequiredError usage-limit /
    switch-model signal are classified as ``cursor_usage_limit``. Timeouts,
    zero exits, and partial matches remain ``unknown``.
    """

    if timed_out or returncode == 0:
        return CursorFailureClassification(code=CursorFailureCode.UNKNOWN)

    parts = [stderr, *structured_errors]
    haystack = "\n".join(part for part in parts if part)
    if not haystack.strip():
        return CursorFailureClassification(code=CursorFailureCode.UNKNOWN)

    if not _ACTION_REQUIRED.search(haystack):
        return CursorFailureClassification(code=CursorFailureCode.UNKNOWN)
    if not _USAGE_LIMIT.search(haystack):
        return CursorFailureClassification(code=CursorFailureCode.UNKNOWN)
    if not _SWITCH_MODEL.search(haystack):
        return CursorFailureClassification(code=CursorFailureCode.UNKNOWN)

    return CursorFailureClassification(
        code=CursorFailureCode.USAGE_LIMIT,
        safe_summary=SAFE_USAGE_LIMIT_SUMMARY,
    )


def is_usage_limit_text(text: str) -> bool:
    """Test helper against an arbitrary stderr-like string."""

    return classify_cursor_failure_text(
        returncode=1,
        timed_out=False,
        stderr=text,
    ).is_usage_limit
