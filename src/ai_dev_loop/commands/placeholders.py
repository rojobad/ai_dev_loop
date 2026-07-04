"""Placeholder commands for later phases."""

from __future__ import annotations

from ai_dev_loop.errors import NotImplementedCommandError


def not_implemented(command: str) -> None:
    raise NotImplementedCommandError(
        f"{command} is not implemented in Phase 1. "
        "This release provides prepare, config validate, and read-only inspection commands."
    )
