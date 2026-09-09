"""Regression tests for Phase 17.4 cursor evidence corrections."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.attempt_envelope import (
    attempt_result_rel,
    attempt_stderr_rel,
    attempt_stdout_rel,
    build_result_envelope,
    envelope_sha256,
    sha256_file,
)
from ai_dev_loop.scheduler.application.cursor_evidence import (
    CursorEvidenceError,
    load_authenticated_cursor_outcome,
    parse_admission_artifact,
    validate_cursor_turn_outcome_semantics,
)
from ai_dev_loop.scheduler.domain.cursor_contract import RUN_CURSOR_TURN_EFFECT_KIND
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    NON_TERMINAL_STATE_KINDS,
    TICK_ELIGIBLE_STATE_KINDS,
)


def test_admission_artifact_parses_frozen_git_paths() -> None:
    text = (
        "branch=main\nhead=abc\ngit_common_dir=/repo/.git\ngit_dir=/repo/.git\nstatus_porcelain=\n"
    )
    parsed = parse_admission_artifact(text)
    assert parsed["git_common_dir"] == "/repo/.git"
    assert parsed["git_dir"] == "/repo/.git"


def test_awaiting_codex_review_is_nonterminal_but_not_tick_eligible() -> None:
    assert "awaiting_codex_review" in NON_TERMINAL_STATE_KINDS
    assert "awaiting_codex_review" not in TICK_ELIGIBLE_STATE_KINDS


def test_authenticated_outcome_rejects_identity_mismatch(tmp_path: Path) -> None:
    attempt_id = "att-test"
    run_root = tmp_path
    stdout_rel = attempt_stdout_rel(attempt_id)
    stderr_rel = attempt_stderr_rel(attempt_id)
    result_rel = attempt_result_rel(attempt_id)
    stdout_path = run_root / stdout_rel
    stderr_path = run_root / stderr_rel
    result_path = run_root / result_rel
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.write_text("", encoding="utf-8")
    stdout_path.write_text('{"effect_kind":"cursor.run_turn"}\n', encoding="utf-8")
    envelope = build_result_envelope(
        attempt_id=attempt_id,
        unit_identity=f"unit-{attempt_id}",
        exit_code=0,
        termination_class=TerminationClass.SUCCESS,
        stdout_artifact_path=stdout_rel,
        stdout_sha256=sha256_file(stdout_path),
        stderr_artifact_path=stderr_rel,
        stderr_sha256=sha256_file(stderr_path),
    )
    result_path.write_bytes(envelope)
    _ = envelope_sha256(envelope)
    with pytest.raises((CursorEvidenceError, ValueError)):
        load_authenticated_cursor_outcome(
            run_root,
            attempt_id="other-attempt",
            unit_identity=f"unit-{attempt_id}",
            result_rel=result_rel,
            stdout_rel=stdout_rel,
            stderr_rel=stderr_rel,
        )


def test_validate_cursor_turn_outcome_rejects_exit_zero_without_parse_ok() -> None:
    outcome = {
        "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
        "returncode": 0,
        "timed_out": False,
        "parse_ok": False,
    }
    with pytest.raises(CursorEvidenceError, match="parseable"):
        validate_cursor_turn_outcome_semantics(outcome)


def test_validate_cursor_turn_outcome_accepts_verified_success() -> None:
    outcome = {
        "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
        "returncode": 0,
        "timed_out": False,
        "parse_ok": True,
        "has_completion_signal": True,
        "failure_code": None,
    }
    validate_cursor_turn_outcome_semantics(outcome)
