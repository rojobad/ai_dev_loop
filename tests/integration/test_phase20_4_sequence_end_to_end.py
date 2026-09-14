"""Integration tests for Phase 20.4 sequence lifecycle end-to-end."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from tests.integration.test_phase20_3_sequence_handoff import (
    BOOTSTRAP_ID,
    THREE_PHASE_RUN_IDS,
    _prepare_three_phase,
    _run_until,
    _tick_service,
)
from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_SEQUENCE_ID

from ai_dev_loop.scheduler.application.sequence_report import SEQUENCE_COMPLETION_REPORT_ARTIFACT
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.domain.sequence import AWAITING_FINALIZATION_SEQUENCE_STATE_KIND
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    del fake_clis
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_three_phase_sequence_writes_completion_report_and_releases_reservation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _prepare_three_phase(git_repo, scheduler_paths)
    start = start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    tick = _tick_service(git_repo, scheduler_paths)
    _run_until(tick, start.run_id, target_kind="completed")
    _run_until(tick, THREE_PHASE_RUN_IDS[1], target_kind="completed")
    _run_until(tick, THREE_PHASE_RUN_IDS[2], target_kind="completed")
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    report_path = artifacts.sequence_root(FIXED_SEQUENCE_ID) / SEQUENCE_COMPLETION_REPORT_ARTIFACT
    assert report_path.is_file()
    report = json.loads(report_path.read_bytes())
    assert report["state_kind"] == "awaiting_finalization"
    assert report["final_run_id"] == THREE_PHASE_RUN_IDS[2]
    assert len(report["final_run_id"]) > 8
    assert report["phases"][0]["run_id"] == THREE_PHASE_RUN_IDS[0]
    assert report["phases"][0]["checkpoint_commit_sha256"]
    assert report["phases"][0]["checkpoint_commit_sha256_prefix"]
    assert report["manual_actions_remaining"] == [
        "final_commit",
        "push",
        "pr_review",
        "merge",
    ]
    assert "prompt" not in report_path.read_text(encoding="utf-8").lower()
    status = SequenceStatusService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifacts=artifacts,
    ).get_status(FIXED_SEQUENCE_ID)
    assert status.state_kind == AWAITING_FINALIZATION_SEQUENCE_STATE_KIND
    assert status.completion_report_sha256_prefix is not None
    assert status.aggregate_counts is not None
    assert status.aggregate_counts.checkpointed == 2
    staged_diff = subprocess.check_output(["git", "diff", "--cached"], cwd=git_repo, text=True)
    assert staged_diff.strip()
    with tick.store.begin_read() as conn:
        reservation = conn.execute(
            "SELECT status FROM scheduler_repository_reservations"
        ).fetchone()
    assert reservation is not None
    assert reservation[0] == "released"
    log = subprocess.check_output(["git", "log", "--oneline"], cwd=git_repo, text=True)
    assert "checkpoint after phase-one" in log
    assert "checkpoint after phase-two" in log
