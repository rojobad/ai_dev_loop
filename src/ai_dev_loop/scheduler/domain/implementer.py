"""Implementer identity helpers for frozen Cursor or Codex selection."""

from __future__ import annotations

CODEX_IMPLEMENTER_NOT_IMPLEMENTED = "codex_implementer_not_implemented"
CODEX_IMPLEMENTER_NOT_IMPLEMENTED_SUMMARY = (
    "Codex implementer selection is frozen, but implementation dispatch is not "
    "available in this phase. No implementer process was launched."
)


def assert_distinct_implementer_and_reviewer_sessions(
    implementer_session_id: str | None,
    reviewer_session_id: str | None,
) -> None:
    """Reject one session serving as both implementer and reviewer B."""

    if (
        implementer_session_id is not None
        and reviewer_session_id is not None
        and implementer_session_id == reviewer_session_id
    ):
        raise ValueError("implementer session identity must stay distinct from reviewer B")
