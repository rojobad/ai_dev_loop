"""Unit tests for state helpers."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.state import (
    RunStatus,
    atomic_write_json,
    generate_run_id,
    sha256_text,
    transition_status,
)


def test_run_id_format() -> None:
    run_id = generate_run_id(
        "fixture-project",
        now=datetime(2026, 7, 4, 13, 45, 12, tzinfo=UTC),
    )
    assert re.fullmatch(r"fixture-project-20260704T134512Z-[0-9a-f]{6}", run_id)


def test_sha256_text() -> None:
    assert (
        sha256_text("hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_atomic_write_json(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_write_json(target, {"schema_version": 1, "status": "prepared"})
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["status"] == "prepared"


def test_status_transitions() -> None:
    transition_status(RunStatus.PREPARED, RunStatus.VALIDATING)
    transition_status(RunStatus.STAGING, RunStatus.REVIEWING)
    transition_status(RunStatus.REVIEWING, RunStatus.COMPLETED)
    transition_status(RunStatus.REVIEWING, RunStatus.WAITING_FOR_CURSOR_FIX)
    transition_status(RunStatus.REVIEWING, RunStatus.MAX_ITERATIONS_REACHED)
    transition_status(RunStatus.WAITING_FOR_CURSOR_FIX, RunStatus.RUNNING_CURSOR)
    transition_status(RunStatus.WAITING_FOR_CURSOR_FIX, RunStatus.MAX_ITERATIONS_REACHED)
    transition_status(RunStatus.INTERRUPTED, RunStatus.VALIDATING)
    transition_status(RunStatus.INTERRUPTED, RunStatus.STAGING)
    transition_status(RunStatus.INTERRUPTED, RunStatus.REVIEWING)
    transition_status(RunStatus.VALIDATING, RunStatus.STAGING)
    transition_status(RunStatus.VALIDATING, RunStatus.REVIEWING)
    for terminal in (
        RunStatus.COMPLETED,
        RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
        RunStatus.MAX_ITERATIONS_REACHED,
        RunStatus.FAILED,
        RunStatus.ABORTED,
    ):
        try:
            transition_status(terminal, RunStatus.VALIDATING)
        except ValueError:
            continue
        raise AssertionError(f"expected terminal status {terminal.value} to reject transitions")
    try:
        transition_status(RunStatus.PREPARED, RunStatus.COMPLETED)
    except ValueError:
        return
    raise AssertionError("expected invalid transition to fail")
